"""Offline synthetic policy evidence; not verification of live Instagram semantics."""
import copy
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import unittest

from app.instagram_collaboration import AcceptedCollaborationEvidence, normalize_collaboration
from app.instagram_scraper.post_ingestion_policy import (
    PostObservation, ProfileLinkEvidence, ErrorIsolationEvidence, evaluate_posts,
)

NOW = datetime(2026, 11, 4, 20, tzinfo=timezone.utc)


def media(code='OfflineCode', author='primary'):
    return dict(code=code, pk='12345', user={'username': author}, media_type=1,
                taken_at=int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()),
                caption={'text': 'Join our workshop November 20, 2026 at 5 PM in CSE B225.'},
                image_versions2={'candidates': [{'url': 'https://test.cdninstagram.com/private.jpg?sig=SECRET'}]})


def observation(node=None, *, profile='primary', errors=False, verified=True, **kwargs):
    node = media() if node is None else node
    evidence = (ProfileLinkEvidence(node.get('code'), node.get('user', {}).get('username'), profile,
                                   'author', 'independent_post_verification') if verified else None)
    return PostObservation(edge={'node': node}, requested_profile=profile, authenticated=True,
                           response_has_errors=errors, profile_evidence=evidence, **kwargs)


class PerPostPolicyTests(unittest.TestCase):
    def evaluate(self, *items, existing=()):
        result = evaluate_posts(list(items), now=NOW, existing_posts=existing)
        self.assertFalse(result['production_ingestion_enabled'])
        self.assertEqual(result['database_writes'], 0)
        for sensitive in ('primary', 'OfflineCode', 'SECRET', 'private.jpg', 'Join our workshop'):
            self.assertNotIn(sensitive, json.dumps(result))
        return result

    def test_valid_ordinary_post_is_offline_eligible_only(self):
        result = self.evaluate(observation())
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertEqual(result['posts'][0]['reasons'], [])

    def test_author_payload_match_and_even_binding_enum_cannot_replace_evidence(self):
        for proof in (None, {'binding': 'verified_match'}, {'source': 'independent_post_verification'}):
            item = observation(verified=False)
            item = PostObservation(item.edge, item.requested_profile, True, False, proof)
            result = self.evaluate(item)
            self.assertEqual(result['counts']['eligible_offline'], 0)
            self.assertIn('profile_link_unverified', result['posts'][0]['reasons'])

    def test_ambiguous_request_provenance_stays_review_required(self):
        proof = ProfileLinkEvidence('OfflineCode', 'primary', 'primary', 'author', 'ambiguous')
        item = PostObservation({'node': media()}, 'primary', True, False, proof)
        self.assertEqual(self.evaluate(item)['posts'][0]['status'], 'review_required')

    def test_confirmed_collaboration_needs_both_link_and_acceptance_evidence(self):
        raw = media(); raw['coauthor_producers'] = [{'username': 'partner'}]
        proof = ProfileLinkEvidence('OfflineCode', 'primary', 'partner', 'confirmed_collaborator',
                                    'reviewed_request_contract')
        item = PostObservation({'node': raw}, 'partner', True, False, proof,
                               (AcceptedCollaborationEvidence('OfflineCode', 'primary', 'partner'),))
        self.assertEqual(self.evaluate(item)['counts']['eligible_offline'], 1)

    def test_unverified_and_pending_collaborations_are_not_accepted(self):
        for field in ('coauthor_producers', 'invited_coauthors'):
            raw = media(); raw[field] = [{'username': 'partner', 'accepted': True}]
            proof = ProfileLinkEvidence('OfflineCode', 'primary', 'partner', 'confirmed_collaborator',
                                       'reviewed_request_contract')
            result = self.evaluate(PostObservation({'node': raw}, 'partner', True, False, proof))
            self.assertIn('collaboration_not_confirmed', result['posts'][0]['reasons'])
            self.assertEqual(result['counts']['eligible_offline'], 0)

    def test_profile_evidence_is_bound_to_post_author_and_requested_profile(self):
        for code, author, profile in (('WrongCode', 'primary', 'primary'),
                                      ('OfflineCode', 'other', 'primary'),
                                      ('OfflineCode', 'primary', 'other')):
            proof = ProfileLinkEvidence(code, author, profile, 'author', 'reviewed_request_contract')
            result = self.evaluate(PostObservation({'node': media()}, 'primary', True, False, proof))
            self.assertIn('profile_link_unverified', result['posts'][0]['reasons'])

    def test_graphql_error_on_unrelated_post_requires_complete_attribution(self):
        evidence = ErrorIsolationEvidence('OfflineCode', 'primary', True, True)
        result = self.evaluate(observation(errors=True, error_isolation=evidence))
        self.assertEqual(result['counts']['eligible_offline'], 1)
        # An unresolved different post remains blocked, without blocking this post.
        other = media('OtherCode'); other['pk'] = '99999'
        result = self.evaluate(observation(errors=True, error_isolation=evidence), observation(other, errors=True))
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertIn('graphql_errors_unresolved_for_post', result['posts'][1]['reasons'])

    def test_required_unknown_incomplete_or_misbound_errors_are_rejected(self):
        cases = [None, ErrorIsolationEvidence('OfflineCode', 'primary', False, True),
                 ErrorIsolationEvidence('OfflineCode', 'primary', True, False),
                 ErrorIsolationEvidence('OtherCode', 'primary', True, True),
                 ErrorIsolationEvidence('OfflineCode', 'other', True, True)]
        for evidence in cases:
            result = self.evaluate(observation(errors=True, error_isolation=evidence))
            self.assertEqual(result['counts']['eligible_offline'], 0)
            self.assertIn('graphql_errors_unresolved_for_post', result['posts'][0]['reasons'])

    def test_partial_timeline_slots_do_not_block_independent_post(self):
        items = [PostObservation({'node': {'media': None}}, 'primary'),
                 PostObservation({'node': {'media': []}}, 'primary'),
                 PostObservation({'node': {'media_or_ad': media()}}, 'primary'), observation()]
        result = self.evaluate(*items)
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertEqual(result['counts']['skipped'], 3)
        self.assertEqual(result['timeline_completeness'], 'not_required')

    def test_missing_identity_caption_image_and_timestamp(self):
        for key in ('code', 'caption', 'image_versions2', 'taken_at'):
            raw = media(); del raw[key]
            result = self.evaluate(observation(raw))
            self.assertEqual(result['counts']['eligible_offline'], 0)
            self.assertTrue(result['posts'][0]['reasons'])

    def test_valid_nested_media_uses_existing_resolver(self):
        item = observation()
        nested = PostObservation({'node': {'media': media()}}, 'primary', True, False, item.profile_evidence)
        self.assertEqual(self.evaluate(nested)['counts']['eligible_offline'], 1)

    def test_image_url_and_media_validation(self):
        for url in ('https://evil.example/private.jpg', 'http://test.cdninstagram.com/private.jpg',
                    'https://user:password@test.cdninstagram.com/private.jpg',
                    'https://test.cdninstagram.com:444/private.jpg'):
            raw = media(); raw['image_versions2']['candidates'][0]['url'] = url
            self.assertEqual(self.evaluate(observation(raw))['counts']['eligible_offline'], 0)
        raw = media(); raw['__typename'] = 'GraphVideo'
        self.assertIn('conflicting_media_type', self.evaluate(observation(raw))['posts'][0]['reasons'])
        for value in (True, 1.0, '1'):
            raw = media(); raw.update(media_type=value, __typename='GraphImage')
            self.assertIn('invalid_media_type', self.evaluate(observation(raw))['posts'][0]['reasons'])

    def test_complete_carousel_and_incomplete_child(self):
        raw = media(); raw.update(media_type=8, carousel_media_count=2,
            carousel_media=[{'pk': '21', 'display_url': 'https://test.cdninstagram.com/a.jpg'},
                            {'pk': '22', 'display_url': 'https://test.cdninstagram.com/b.jpg'}])
        self.assertEqual(self.evaluate(observation(raw))['counts']['eligible_offline'], 1)
        del raw['carousel_media'][1]['display_url']
        self.assertIn('missing_carousel_child_image', self.evaluate(observation(raw))['posts'][0]['reasons'])
        raw['carousel_media_count'] = 3
        self.assertIn('carousel_count_unverified', self.evaluate(observation(raw))['posts'][0]['reasons'])

    def test_sidebar_carousel_pagination_must_be_complete(self):
        raw = media(); raw['media_type'] = 8
        raw['edge_sidecar_to_children'] = dict(count=1, edges=[{'node': {'pk': '21',
            'display_url': 'https://test.cdninstagram.com/a.jpg'}}], page_info={'has_next_page': False})
        self.assertEqual(self.evaluate(observation(raw))['counts']['eligible_offline'], 1)
        raw['edge_sidecar_to_children']['page_info']['has_next_page'] = True
        self.assertIn('carousel_pagination_unverified', self.evaluate(observation(raw))['posts'][0]['reasons'])

    def test_duplicates_do_not_merge_or_produce_two_candidates(self):
        result = self.evaluate(observation(), observation())
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertIn('duplicate_post_identity', result['posts'][1]['reasons'])

    def test_cdn_signature_changes_are_not_identity_conflicts(self):
        other = media(); other['image_versions2']['candidates'][0]['url'] += 'NEW'
        result = self.evaluate(observation(), observation(other))
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertEqual(result['posts'][1]['reasons'], ['duplicate_post_identity'])

    def test_incomplete_duplicate_does_not_crowd_out_complete_observation(self):
        raw = media(); del raw['image_versions2']
        result = self.evaluate(observation(raw), observation())
        self.assertEqual(result['posts'][1]['status'], 'eligible_offline')

    def test_conflicts_quarantine_all_same_identity_observations(self):
        missing_id = media(); del missing_id['pk']
        conflicting = media(); conflicting['pk'] = '99999'
        result = self.evaluate(observation(), observation(missing_id), observation(conflicting))
        self.assertEqual(result['counts']['eligible_offline'], 0)
        self.assertTrue(all('post_identity_or_data_conflict' in item['reasons'] for item in result['posts']))

    def test_same_post_with_conflicting_captions_requires_review(self):
        raw = media(); raw['caption']['text'] += ' Different description.'
        result = self.evaluate(observation(), observation(raw))
        self.assertEqual(result['counts']['review_required'], 2)

    def test_existing_parent_identity_is_skipped_without_rekeying(self):
        result = self.evaluate(observation(), existing=[normalize_collaboration(media(), 'primary')])
        self.assertEqual(result['counts']['eligible_offline'], 0)
        self.assertIn('existing_post_identity', result['posts'][0]['reasons'])
        conflicting = media(); conflicting['pk'] = '99999'
        result = self.evaluate(observation(), existing=[normalize_collaboration(conflicting)])
        self.assertIn('existing_identity_conflict', result['posts'][0]['reasons'])

    def test_cross_profile_duplicate_cannot_create_two_offline_candidates(self):
        proof = ProfileLinkEvidence('OfflineCode', 'primary', 'partner', 'confirmed_collaborator',
                                   'independent_post_verification')
        collab = PostObservation({'node': media()}, 'partner', True, False, proof,
                                 (AcceptedCollaborationEvidence('OfflineCode', 'primary', 'partner'),))
        result = self.evaluate(observation(), collab)
        self.assertEqual(result['counts']['eligible_offline'], 1)
        self.assertIn('duplicate_post_identity', result['posts'][1]['reasons'])

    def test_freshness_and_event_screening_remain_required(self):
        raw = media(); raw['taken_at'] = int((NOW - timedelta(days=41)).timestamp())
        self.assertIn('post_outside_freshness_window', self.evaluate(observation(raw))['posts'][0]['reasons'])
        raw = media(); raw['caption']['text'] = 'Workshop October 24, 2026.'
        self.assertIn('event_expired', self.evaluate(observation(raw))['posts'][0]['reasons'])
        raw['caption']['text'] = 'Join our workshop soon.'
        self.assertIn('event_screening_requires_review', self.evaluate(observation(raw))['posts'][0]['reasons'])

    def test_publication_boundary_and_timezone_aware_clock(self):
        raw = media(); raw['taken_at'] = int((NOW - timedelta(days=40)).timestamp())
        self.assertEqual(self.evaluate(observation(raw))['counts']['eligible_offline'], 1)
        raw['taken_at'] = int((NOW + timedelta(hours=1)).timestamp())
        self.assertIn('publication_in_future', self.evaluate(observation(raw))['posts'][0]['reasons'])
        with self.assertRaises(ValueError):
            evaluate_posts([], now=NOW.replace(tzinfo=None))

    def test_authentication_missing_or_conflicting_author_blocks_post(self):
        raw = media(); raw['owner'] = {'username': 'other'}
        self.assertIn('author_owner_conflict', self.evaluate(observation(raw))['posts'][0]['reasons'])
        item = observation()
        result = self.evaluate(PostObservation(item.edge, 'primary', False, False, item.profile_evidence))
        self.assertIn('authentication_unverified', result['posts'][0]['reasons'])

    def test_raw_partial_diagnostics_never_become_acceptance_evidence(self):
        result = self.evaluate({'status': 'partial_unverified', 'fully_valid_candidates': 63})
        self.assertEqual(result['posts'][0]['reasons'], ['malformed_observation'])

    def test_input_unchanged_and_no_raw_values_in_output(self):
        raw = media(); before = copy.deepcopy(raw)
        self.evaluate(observation(raw))
        self.assertEqual(raw, before)

    def test_fresh_process_import_does_not_load_database_browser_or_downstream(self):
        program = '''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('Blueprint_db','sqlalchemy','psycopg2','pymysql','playwright','boto3','openai') or any(
            part in fullname for part in ('ai_analyze','event_cover_service','event_master_sync','pipeline_recovery',
                'parent_pipeline.service','parent_pipeline.integration','attempt_safety','daily_budget')):
            raise AssertionError('forbidden import')
sys.meta_path.insert(0, Guard())
from app.instagram_scraper.post_ingestion_policy import evaluate_posts
from datetime import datetime, timezone
assert evaluate_posts([], now=datetime.now(timezone.utc))['production_ingestion_enabled'] is False
'''
        completed = subprocess.run([sys.executable, '-c', program], capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, 'Pure policy import must remain isolated')
