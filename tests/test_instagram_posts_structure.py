"""Synthetic div topologies are inspection candidates, never membership proof."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from app.instagram_scraper.profile_posts_structure import project_structure, inspect_posts_structure
from app.instagram_scraper.profile_grid_evidence import capture_membership


def candidate(**changes):
    value = dict(tag='div', relation='adjacent_after', attributes=[], depth_from_main=2,
        contains_header=False, contains_excluded_region=False, contains_nonpost_links=False,
        branches_truncated=False, post_links=12, other_profile_links=0, child_branches=4,
        post_branches=4, minimum_links_per_post_branch=3, maximum_links_per_post_branch=3)
    value.update(changes)
    return value


def structure(**changes):
    value = dict(navigation_matches=True,selection='posts',navigation_kind='common_ancestor',
        counts=dict(main=1,matching_headers=1,profile_navigation_links=3,selected_navigation_links=1,
                    visible_post_links=12,excluded_post_links=0,candidate_ancestors=1),
        patterns=dict(requested_profile_post=12),candidates=[candidate()],truncated=False)
    value.update(changes)
    return value


class PostsStructureTests(unittest.TestCase):
    def project(self,value):
        result = project_structure(value)
        self.assertFalse(result['membership_evidence'])
        self.assertFalse(result['production_ingestion_enabled'])
        self.assertNotIn('SECRET',json.dumps(result))
        return result

    def test_valid_div_rows_without_aria_roles_are_candidates_only(self):
        result = self.project(structure())
        self.assertEqual(result['reason'],'container_candidate_requires_review')
        self.assertEqual(result['candidates'][0]['post_branches'],4)
        self.assertEqual(result['candidates'][0]['tag'],'div')
        self.assertEqual(result['candidates'][0]['attributes'],[])

    def test_loose_links_without_navigation_boundary_are_insufficient(self):
        value = structure(candidates=[candidate(relation='after_different_parent')])
        self.assertEqual(self.project(value)['reason'],'posts_boundary_unresolved')

    def test_unrelated_content_and_header_in_container_are_not_review_candidates(self):
        for changes in (dict(contains_excluded_region=True), dict(contains_nonpost_links=True),
                        dict(contains_header=True), dict(other_profile_links=1), dict(post_branches=1)):
            self.assertEqual(self.project(structure(candidates=[candidate(**changes)]))['reason'],
                             'posts_boundary_unresolved')

    def test_missing_reels_tagged_or_multiple_selection_does_not_establish_posts(self):
        for selection in ('missing','reels','tagged','ambiguous','other'):
            self.assertEqual(self.project(structure(selection=selection))['reason'],'posts_selection_unverified')

    def test_ambiguous_ancestor_candidates_are_reported_without_choosing_one(self):
        result = self.project(structure(candidates=[candidate(),candidate(depth_from_main=3)]))
        self.assertEqual(result['reason'],'ambiguous_container_candidates')

    def test_duplicate_anonymous_shapes_keep_multiplicity_not_fake_unique_boundary(self):
        result = self.project(structure(candidates=[candidate(),candidate()]))
        self.assertEqual(len(result['candidates']),1)
        self.assertEqual(result['candidates'][0]['count'],2)
        self.assertEqual(result['reason'],'ambiguous_container_candidates')

    def test_container_before_or_containing_navigation_does_not_qualify(self):
        for relation in ('before','contains_navigation','inside_navigation','unavailable'):
            result = self.project(structure(candidates=[candidate(relation=relation)]))
            self.assertEqual(result['reason'],'posts_boundary_unresolved')

    def test_truncation_prevents_a_complete_structural_conclusion(self):
        self.assertEqual(self.project(structure(truncated=True))['reason'],'structural_inspection_truncated')
        result = self.project(structure(candidates=[candidate(branches_truncated=True)]))
        self.assertEqual(result['reason'],'posts_boundary_unresolved')

    def test_unknown_fields_values_and_attribute_values_never_leave_projection(self):
        value = structure(secret='SECRET_RESPONSE',selection='SECRET_USERNAME',
            navigation_kind='SECRET_ID',patterns={'SECRET_URL':50},
            candidates=[candidate(tag='SECRET_TAG',relation='SECRET_PATH',
                attributes=['role','SECRET_ATTRIBUTE'],username='SECRET_USERNAME',
                id='SECRET_ID',className='SECRET_CLASS',href='SECRET_URL',caption='SECRET_CAPTION')])
        result = self.project(value)
        self.assertEqual(result['selection'],'missing')
        self.assertEqual(result['candidates'][0]['attributes'],['role'])

    def test_malformed_or_negative_counts_do_not_create_candidates(self):
        value = structure(counts={'main':True,'matching_headers':-1},
                          candidates=[None,candidate(post_branches='SECRET',post_links=-9)])
        result = self.project(value)
        self.assertEqual(result['counts']['main'],0)
        self.assertEqual(result['candidates'][0]['post_links'],0)
        self.assertEqual(result['reason'],'profile_main_unverified')

    def test_saved_live_fixture_does_not_supply_missing_selection_or_relationships(self):
        old = json.loads((Path(__file__).parent/'fixtures/profile_dom_inspection_structure.json').read_text())['structure']
        # Map only observed counts. Do not invent selection, ancestry or boundary.
        result = self.project(dict(counts={'main':old['visible_main_count'],
            'matching_headers':old['exact_profile_header_matches'],
            'visible_post_links':old['visible_post_link_count']}))
        self.assertEqual(result['counts']['visible_post_links'],12)
        self.assertEqual(result['reason'],'posts_selection_unverified')

    def test_read_only_inspection_checks_context_before_and_after(self):
        page = Mock(); nav=dict(url='https://www.instagram.com/primary/',epoch=123456789.0)
        page.evaluate.side_effect=[nav,structure(),nav]
        result = inspect_posts_structure(page,'primary')
        self.assertEqual(result['reason'],'container_candidate_requires_review')
        page.goto.assert_not_called();page.request.get.assert_not_called()
        self.assertEqual(page.evaluate.call_count,3)

    def test_navigation_change_and_login_redirect_discard_candidates(self):
        nav=dict(url='https://www.instagram.com/primary/',epoch=123456789.0)
        for after in (dict(nav,epoch=987654321.0),dict(nav,url='https://www.instagram.com/other/')):
            page=Mock();page.evaluate.side_effect=[nav,structure(),after]
            result=inspect_posts_structure(page,'primary')
            self.assertEqual(result['reason'],'navigation_context_changed')
            self.assertEqual(result['candidates'],[])
        page=Mock();page.evaluate.return_value=dict(nav,url='https://www.instagram.com/accounts/login/')
        self.assertEqual(inspect_posts_structure(page,'primary')['reason'],'navigation_context_unverified')
        self.assertEqual(page.evaluate.call_count,1)

    def test_exception_text_is_not_exposed(self):
        page=Mock();page.evaluate.side_effect=RuntimeError('SECRET_CREDENTIAL')
        result=inspect_posts_structure(page,'primary')
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertEqual(result['candidates'],[])

    def test_no_contract_is_promoted_from_plausible_structure(self):
        self.project(structure())
        page=Mock()
        evidence,summary=capture_membership(page,'primary',now=datetime.now(timezone.utc),authenticated=True)
        self.assertIsNone(evidence);page.evaluate.assert_not_called()
        self.assertEqual(summary['reason'],'reviewed_grid_contract_missing')

    def test_projection_does_not_mutate_input(self):
        value=structure();before=copy.deepcopy(value)
        self.project(value)
        self.assertEqual(value,before)

    def test_import_has_no_browser_database_or_downstream_dependencies(self):
        program='''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('Blueprint_db','sqlalchemy','psycopg2','pymysql','playwright','boto3','openai') or any(
            part in fullname for part in ('ai_analyze','event_cover_service','event_master_sync','pipeline_recovery',
                'parent_pipeline.service','parent_pipeline.integration','attempt_safety','daily_budget')):
            raise AssertionError('forbidden import')
sys.meta_path.insert(0,Guard())
from app.instagram_scraper.profile_posts_structure import project_structure
assert project_structure({})['membership_evidence'] is False
'''
        result=subprocess.run([sys.executable,'-c',program],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,'Structural inspector must remain isolated')
