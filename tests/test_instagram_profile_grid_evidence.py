"""Synthetic reviewed-grid fixture; does not attest to Instagram's live DOM."""
import copy
from dataclasses import replace
from datetime import timedelta
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock

from test_instagram_post_ingestion_policy import media, NOW
from app.instagram_collaboration import AcceptedCollaborationEvidence
from app.instagram_scraper.profile_grid_evidence import (
    ReviewedGridContract, NavigationContext, capture_membership, current_navigation, evaluate_response,
)

ROOT = 'xdt_api__v1__feed__user_timeline_graphql_connection'
CONTRACT = ReviewedGridContract('synthetic-offline-fixture-only', '[data-test=profile-name]',
    '[data-test=posts-tab]', '[data-test=posts-panel]', '[data-test=post-cell]')


def snapshot(profile='primary', links=None):
    return dict(url=f'https://www.instagram.com/{profile}/', epoch=123456789.0,
        header=profile, tab_href=f'/{profile}/', scoped=True, truncated=False,
        cells=[dict(scoped=True, truncated=False, links=links or ['/p/OfflineCode/'])])


def payload(nodes=None, errors=None):
    result = {'data': {ROOT: {'edges': [{'node': node} for node in (nodes or [media()])]}}}
    if errors is not None:
        result['errors'] = errors
    return result


class ProfileGridEvidenceTests(unittest.TestCase):
    def capture(self, data=None, *, before=None, after=None, contract=CONTRACT, authenticated=True, profile='primary'):
        data = snapshot(profile) if data is None else data
        nav = dict(url=data.get('url'), epoch=data.get('epoch'))
        page = Mock(); page.evaluate.side_effect = [before or nav, data, after or nav]
        evidence, summary = capture_membership(page, profile, now=NOW, contract=contract,
                                                authenticated=authenticated)
        for secret in ('primary', 'OfflineCode', 'instagram.com', 'synthetic-offline-fixture-only'):
            self.assertNotIn(secret, json.dumps(summary))
        return evidence, summary, page

    def evaluate(self, data=None, *, evidence=None, context=None, profile='primary', **kwargs):
        if evidence is None:
            evidence, _, _ = self.capture(profile=profile)
        result = evaluate_response(payload() if data is None else data, membership=evidence,
            navigation=context or evidence.context, requested_profile=profile, now=NOW, **kwargs)
        self.assertFalse(result['production_ingestion_enabled'])
        for value in ('OfflineCode', 'PRIVATE', 'SECRET', 'instagram.com', 'private.jpg'):
            self.assertNotIn(value, json.dumps(result))
        return result

    def test_default_has_no_live_contract_and_does_not_touch_browser(self):
        evidence, summary, page = self.capture(contract=None)
        self.assertIsNone(evidence)
        self.assertEqual(summary['reason'], 'reviewed_grid_contract_missing')
        page.evaluate.assert_not_called()

    def test_scoped_grid_exact_code_produces_ordinary_post_evidence(self):
        evidence, summary, _ = self.capture()
        self.assertEqual(summary['verified_cells'], 1)
        self.assertEqual(evidence.shortcodes, frozenset(('OfflineCode',)))
        self.assertEqual(self.evaluate(evidence=evidence)['counts']['eligible_offline'], 1)

    def test_unrelated_suggestion_not_in_grid_is_not_verified(self):
        evidence, _, _ = self.capture(snapshot(links=['/p/OtherCode/']))
        result = self.evaluate(evidence=evidence)
        self.assertEqual(result['counts']['eligible_offline'], 0)
        self.assertIn('profile_link_unverified', result['posts'][0]['reasons'])

    def test_unscoped_suggested_or_nested_panel_cell_is_excluded(self):
        data = snapshot(); data['cells'][0]['scoped'] = False
        evidence, summary, _ = self.capture(data)
        self.assertEqual(summary['skipped_cells'], 1)
        self.assertEqual(self.evaluate(evidence=evidence)['counts']['eligible_offline'], 0)

    def test_ambiguous_dom_links_are_not_membership(self):
        for links in (['/p/OfflineCode/', '/p/OtherCode/'], ['/p/OfflineCode/', '/suggestions/'],
                      ['https://evil.example/p/OfflineCode/'], ['/p/OfflineCode/?private=x'],
                      ['/p/OfflineCode/#private'], ['/p/%4FfflineCode/'], [None]):
            evidence, summary, _ = self.capture(snapshot(links=links))
            self.assertEqual(summary['skipped_cells'], 1)
            self.assertEqual(len(evidence.shortcodes), 0)

    def test_multiple_links_to_exact_same_code_in_one_cell_are_not_ambiguous(self):
        evidence, summary, _ = self.capture(snapshot(links=['/p/OfflineCode/',
            'https://www.instagram.com/p/OfflineCode/']))
        self.assertEqual(summary['verified_cells'], 1)
        self.assertEqual(len(evidence.shortcodes), 1)

    def test_header_posts_tab_and_grid_scope_must_all_match(self):
        for key, value in (('header', 'someone_else'), ('tab_href', '/primary/tagged/'),
                           ('tab_href', '/other/'), ('scoped', False)):
            data = snapshot(); data[key] = value
            evidence, summary, _ = self.capture(data)
            self.assertIsNone(evidence)
            self.assertEqual(summary['reason'], 'profile_posts_grid_unverified')

    def test_navigation_change_including_same_url_reload_is_rejected(self):
        for after in ({'url': 'https://www.instagram.com/other/', 'epoch': 123456789.0},
                      {'url': 'https://www.instagram.com/primary/', 'epoch': 987654321.0}):
            evidence, summary, _ = self.capture(after=after)
            self.assertIsNone(evidence)
            self.assertEqual(summary['reason'], 'navigation_context_mismatch')

    def test_login_redirect_or_other_origin_is_not_a_profile_context(self):
        for url in ('https://www.instagram.com/accounts/login/', 'https://evil.example/primary/',
                    'https://user:password@www.instagram.com/primary/', 'http://www.instagram.com/primary/'):
            data = snapshot(); data['url'] = url
            evidence, _, _ = self.capture(data)
            self.assertIsNone(evidence)

    def test_missing_authentication_never_reads_or_accepts_grid(self):
        evidence, summary, page = self.capture(authenticated=False)
        self.assertIsNone(evidence); page.evaluate.assert_not_called()
        self.assertEqual(summary['reason'], 'profile_or_authentication_unverified')

    def test_stale_or_misbound_snapshot_cannot_authorize_post(self):
        evidence, _, _ = self.capture()
        for context in (NavigationContext('other', evidence.context.document_epoch),
                        NavigationContext('primary', evidence.context.document_epoch + 1)):
            self.assertEqual(self.evaluate(evidence=evidence, context=context)['counts']['eligible_offline'], 0)
        stale = replace(evidence, observed_at=NOW-timedelta(minutes=6))
        self.assertEqual(self.evaluate(evidence=stale)['counts']['eligible_offline'], 0)

    def test_malformed_or_future_snapshot_clock_is_not_usable(self):
        evidence, _, _ = self.capture()
        for stamp in ('PRIVATE', NOW.replace(tzinfo=None), NOW+timedelta(minutes=1)):
            invalid = replace(evidence, observed_at=stamp)
            self.assertEqual(self.evaluate(evidence=invalid)['counts']['eligible_offline'], 0)

    def test_inspection_truncation_does_not_assert_complete_coverage(self):
        data = snapshot(); data['truncated'] = True
        data['cells'].append(dict(scoped=True, truncated=True, links=['/p/OtherCode/']))
        evidence, summary, _ = self.capture(data)
        self.assertTrue(summary['truncated']); self.assertEqual(summary['skipped_cells'], 1)
        self.assertEqual(self.evaluate(evidence=evidence)['counts']['eligible_offline'], 1)

    def test_grid_membership_alone_does_not_confirm_collaboration(self):
        raw = media(author='other'); raw['coauthor_producers'] = [{'username': 'primary'}]
        result = self.evaluate(payload([raw]))
        self.assertEqual(result['counts']['eligible_offline'], 0)
        self.assertEqual(result['evidence_adapter']['counts']['collaboration_unconfirmed'], 1)

    def test_confirmed_collaboration_preserves_primary_and_profile_identities(self):
        raw = media(author='other')
        result = self.evaluate(payload([raw]), collaboration_evidence=(
            AcceptedCollaborationEvidence('OfflineCode', 'other', 'primary'),))
        self.assertEqual(result['counts']['eligible_offline'], 1)
        # A confirmation for another primary cannot leak across identities.
        result = self.evaluate(payload([raw]), collaboration_evidence=(
            AcceptedCollaborationEvidence('OfflineCode', 'wrong', 'primary'),))
        self.assertEqual(result['counts']['eligible_offline'], 0)

    def test_matching_author_without_membership_is_insufficient(self):
        evidence, _, _ = self.capture(snapshot(links=['/p/OtherCode/']))
        self.assertEqual(self.evaluate(evidence=evidence)['counts']['eligible_offline'], 0)

    def test_unknown_error_paths_remain_unresolved_even_on_other_nodes(self):
        other = media('OtherCode'); other['pk'] = '99999'
        errors = [{'path': [ROOT, 'edges', 1, 'node', 'PRIVATE_UNKNOWN'], 'message': 'PRIVATE_ERROR'}]
        result = self.evaluate(payload([media(), other], errors))
        self.assertEqual(result['counts']['eligible_offline'], 0)
        self.assertEqual(result['evidence_adapter']['counts']['errors_unresolved'], 2)

    def test_reviewed_disjoint_consumed_field_path_can_isolate_error(self):
        other = media('OtherCode'); other['pk'] = '99999'; other['caption'] = None
        errors = [{'path': [ROOT, 'edges', 1, 'node', 'caption'], 'message': 'PRIVATE_ERROR'}]
        result = self.evaluate(payload([media(), other], errors))
        self.assertEqual(result['posts'][0]['status'], 'eligible_offline')
        self.assertNotEqual(result['posts'][1]['status'], 'eligible_offline')

    def test_own_required_errors_ancestors_pagination_and_malformed_paths_block(self):
        for path in ([ROOT, 'edges', 0, 'node', 'caption'], [ROOT, 'edges'], [ROOT, 'page_info'],
                     [ROOT, 'edges', 999, 'node', 'caption'], ['other', 'edges', 1, 'node', 'caption'],
                     None, 'PRIVATE', [ROOT, 'edges', True, 'node', 'caption']):
            result = self.evaluate(payload(errors=[{'path': path}]))
            self.assertEqual(result['counts']['eligible_offline'], 0)

    def test_error_attribution_does_not_accept_nonexistent_ancestor(self):
        other = media('OtherCode'); other['pk'] = '99999'; other.pop('caption')
        result = self.evaluate(payload([media(), other], errors=[
            {'path': [ROOT, 'edges', 1, 'node', 'caption', 'text']}]))
        self.assertEqual(result['counts']['eligible_offline'], 0)

    def test_partial_timeline_nulls_and_bad_slots_do_not_block_verified_post(self):
        result = self.evaluate(payload([media(), {'media': None}, {'media': []}, {'unsupported': True}]))
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertEqual(result['counts']['skipped'], 3)

    def test_missing_fields_and_duplicates_still_use_existing_policy(self):
        raw = media(); raw.pop('caption')
        self.assertEqual(self.evaluate(payload([raw]))['counts']['eligible_offline'], 0)
        result = self.evaluate(payload([media(), media()]))
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertIn('duplicate_post_identity', result['posts'][1]['reasons'])

    def test_malformed_grid_reads_fail_safely(self):
        data = snapshot(); data['cells'] = 'PRIVATE'
        evidence, summary, _ = self.capture(data)
        self.assertIsNone(evidence); self.assertEqual(summary['reason'], 'malformed_grid_snapshot')
        page = Mock(); page.evaluate.side_effect = RuntimeError('PRIVATE_ERROR')
        evidence, summary = capture_membership(page, 'primary', now=NOW, contract=CONTRACT, authenticated=True)
        self.assertIsNone(evidence); self.assertNotIn('PRIVATE', json.dumps(summary))

    def test_normal_discovery_and_partial_capture_are_not_invoked(self):
        # Only page.evaluate is accessible through this mocked reader; no browser
        # navigation, request capture, DB or downstream entry point is needed.
        evidence, _, page = self.capture()
        self.evaluate(evidence=evidence)
        page.goto.assert_not_called(); page.request.get.assert_not_called()

    def test_no_raw_data_is_retained_in_public_output_and_input_is_unchanged(self):
        value = payload(); before = copy.deepcopy(value)
        self.evaluate(value)
        self.assertEqual(value, before)

    def test_navigation_read_failure_returns_no_context(self):
        page = Mock(); page.evaluate.side_effect = RuntimeError('PRIVATE_ERROR')
        self.assertIsNone(current_navigation(page))

    def test_import_has_no_browser_database_or_downstream_dependencies(self):
        program = '''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('Blueprint_db','sqlalchemy','psycopg2','pymysql','playwright','boto3','openai') or any(
            part in fullname for part in ('ai_analyze','event_cover_service','event_master_sync','pipeline_recovery',
                'parent_pipeline.service','parent_pipeline.integration','attempt_safety','daily_budget')):
            raise AssertionError('forbidden import')
sys.meta_path.insert(0, Guard())
from app.instagram_scraper.profile_grid_evidence import capture_membership
from datetime import datetime, timezone
evidence, summary = capture_membership(None, 'primary', now=datetime.now(timezone.utc))
assert evidence is None and summary['reason'] == 'reviewed_grid_contract_missing'
'''
        completed = subprocess.run([sys.executable, '-c', program], capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, 'Evidence adapter import must remain isolated')
