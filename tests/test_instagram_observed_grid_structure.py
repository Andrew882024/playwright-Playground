"""Sanitized single live DOM observation: counts cannot prove grid membership."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from test_instagram_post_ingestion_policy import NOW, media
from app.instagram_scraper.profile_grid_evidence import (
    ReviewedGridContract, NavigationContext, capture_membership, evaluate_response, _permalink,
)

FIXTURE = Path(__file__).parent / 'fixtures/profile_dom_inspection_structure.json'


class ObservedGridStructureTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads(FIXTURE.read_text())
        self.structure = self.record['structure']

    def test_fixture_contains_only_fixed_structural_values(self):
        allowed = {'inspected', 'h1', 'h2', 'none_or_other', 'div', 'tabpanel', 'role',
                   'a', 'link', 'href', 'aria-selected'}
        def check(value):
            if isinstance(value, dict):
                for item in value.values():check(item)
            elif isinstance(value, list):
                for item in value:check(item)
            elif isinstance(value, str):self.assertIn(value, allowed)
            else:self.assertIn(type(value), (int, bool))
        check(self.record)
        self.assertNotIn('dvcatucsd', FIXTURE.read_text())
        self.assertEqual(self.record['navigation_attempts'], 1)
        for key in ('database_writes', 'gemini_requests', 'openai_requests'):
            self.assertEqual(self.record[key], 0)

    def test_matching_header_and_loose_links_do_not_prove_scoped_grid(self):
        shape = self.structure
        self.assertEqual(shape['exact_profile_header_matches'], 1)
        self.assertEqual(shape['visible_post_link_count'], 12)
        self.assertEqual(shape['role_counts']['tab'], 0)
        self.assertEqual(shape['role_counts']['gridcell'], 0)
        self.assertEqual(shape['linked_selected_tab_panel_count'], 0)
        self.assertTrue(all(not item['inside_panel'] and not item['inside_grid']
                            for item in shape['post_link_ancestor_shapes']))
        self.assertNotIn('aria-labelledby', shape['panel_shapes'][0]['attributes'])

    def test_observed_missing_relationships_fail_closed_in_reader_and_policy(self):
        # This reconstructs the reader's missing structural prerequisites using
        # synthetic navigation, not a raw DOM fixture or real post/account values.
        shape = self.structure
        scoped = (shape['role_counts']['tab'] > 0 and shape['linked_selected_tab_panel_count'] > 0)
        nav = dict(url='https://www.instagram.com/primary/', epoch=123456789.0)
        snapshot = dict(nav, header='primary', tab_href=None, scoped=scoped,
                        cells=[], truncated=False)
        page = Mock(); page.evaluate.side_effect = [nav, snapshot, nav]
        contract = ReviewedGridContract('offline-observed-rejection-fixture', 'header h2',
            '[role="tab"][aria-selected="true"]', '[role="tabpanel"]', '[role="gridcell"]')
        evidence, summary = capture_membership(page, 'primary', now=NOW, contract=contract, authenticated=True)
        self.assertIsNone(evidence)
        self.assertEqual(summary['reason'], 'profile_posts_grid_unverified')
        result = evaluate_response({'data': {'profile_grid': {'edges': [{'node': media()}]}}},
            membership=evidence, navigation=NavigationContext('primary', nav['epoch']),
            requested_profile='primary', now=NOW)
        self.assertEqual(result['counts']['eligible_offline'], 0)
        self.assertFalse(result['production_ingestion_enabled'])

    def test_no_live_contract_is_adopted_from_structural_counts(self):
        page = Mock()
        evidence, summary = capture_membership(page, 'primary', now=NOW, authenticated=True)
        self.assertIsNone(evidence)
        self.assertEqual(summary['reason'], 'reviewed_grid_contract_missing')
        page.evaluate.assert_not_called()

    def test_profile_scoped_link_pattern_is_not_silently_promoted(self):
        self.assertEqual(self.structure['permalink_patterns']['profile_p_shortcode'], 12)
        self.assertIsNone(_permalink('/primary/p/OfflineCode/'))
        self.assertEqual(_permalink('/p/OfflineCode/'), 'OfflineCode')
