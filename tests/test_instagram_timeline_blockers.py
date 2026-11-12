"""Offline blocker regressions; nulls and GraphQL errors never gain acceptance."""
import json
import unittest

import test_instagram_timeline_media as media_tests
from test_instagram_timeline_media import media
from test_instagram_graphql_attribution import payload, ROOT
from app.instagram_scraper import sync_ins as scraper, graphql_diagnostics as diag


class TimelineBlockerTests(unittest.TestCase):
    capture = media_tests.TimelineMediaTests.capture

    def test_observed_one_nested_three_null_slots_still_requires_evidence(self):
        data=payload({'media':media()})
        data['data'][ROOT]['edges'] += [{'node':{'media':None}} for _ in range(3)]
        self.assertEqual(len(scraper._extract_timeline_nodes(data)),1)
        health,bodies,codes=self.capture(data)
        self.assertEqual(health['error_categories']['unsupported_timeline_schema'],1)
        self.assertEqual(health['valid_timeline_responses'],0)
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())
        patterns=health['timeline_wrapper_diagnostics']['patterns']
        self.assertEqual(sum(p['count'] for p in patterns if p['wrapper_kind']=='node_media'
                             and p['candidate_is_null']),3)

    def test_null_only_known_and_unknown_connections_are_never_empty_success(self):
        for root in (ROOT,'unknown_connection'):
            data={'data':{root:{'edges':[{'node':{'media':None}}]}}}
            health,bodies,_=self.capture(data)
            self.assertEqual(health['valid_empty_timeline_responses'],0)
            self.assertEqual(health['valid_timeline_responses'],0)
            self.assertIsNotNone(scraper.capture_failure(health));self.assertEqual(bodies,[])

    def test_valid_sibling_does_not_mask_malformed_or_unknown_wrappers(self):
        for node in ({'media':[]},{'media':'PRIVATE_TEST'},{'media':42},None,
                     {'media_or_ad':media()},{'unknown':media()},{}):
            data=payload({'media':media()});data['data'][ROOT]['edges'].append({'node':node})
            health,bodies,codes=self.capture(data)
            self.assertIsNotNone(scraper.capture_failure(health))
            self.assertEqual(bodies,[]);self.assertEqual(codes,set())

    def test_required_ancestor_with_unknown_descendant_retains_uncertainty(self):
        for field,category in (('caption','timeline_caption'),('user','timeline_organizer'),
            ('image_versions2','timeline_media'),('preview_comments','timeline_comments'),
            ('is_pinned','timeline_pinning'),('device_timestamp','timeline_time')):
            for wrapper in ([],['media']):
                path=[ROOT,'edges',0,'node',*wrapper,field,'PRIVATE_TEST']
                result=diag.classify_error_path(path)
                self.assertEqual(result['category'],category)
                self.assertTrue(result['unknown_below_required_field'])
                self.assertTrue(result['contains_unknown_segments'])
                self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_structural_error_endpoints(self):
        for tail,category,flag in ((['edges',0],'timeline_edge','ends_at_edge'),
            (['edges',0,'node'],'timeline_node','ends_at_node'),
            (['edges',0,'node','media'],'timeline_wrapper','ends_at_media_wrapper'),
            (['edges',0,'node','media_or_ad','media'],'timeline_wrapper','ends_at_media_wrapper')):
            result=diag.classify_error_path([ROOT,*tail])
            self.assertEqual(result['category'],category);self.assertTrue(result[flag])

    def test_unknown_scope_is_attributed_without_disclosing_names(self):
        for tail,flag in ((['PRIVATE_TEST','edges',0,'node','code'],'unknown_at_connection'),
                          (['edges',0,'node','PRIVATE_TEST'],'unknown_at_node'),
                          (['edges',0,'node','media','PRIVATE_TEST'],'unknown_at_node')):
            result=diag.classify_error_path([ROOT,*tail])
            self.assertEqual(result['category'],'timeline_unknown_field')
            self.assertTrue(result[flag]);self.assertTrue(result['contains_unknown_segments'])
            self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_optional_leaf_is_not_inferred_from_unknown_ancestor_or_descendant(self):
        for tail in (['PRIVATE_TEST','like_count'],['like_count','PRIVATE_TEST']):
            result=diag.classify_error_path([ROOT,'edges',0,'node',*tail])
            self.assertEqual(result['category'],'timeline_unknown_field')
        result=diag.classify_error_path([ROOT,'edges',0,'node','like_count'])
        self.assertEqual(result['category'],'optional_node_field_candidate')

    def test_required_and_unknown_correlations_coexist_and_errors_rejected(self):
        data=payload(media())
        data['errors']=[{'path':[ROOT,'edges',0,'node','caption','PRIVATE_TEST']}]
        health,bodies,codes=self.capture(data)
        counts=health['graphql_error_timeline_correlation']
        for key in ('with_critical_paths','with_unknown_paths','with_recognized_media'):
            self.assertEqual(counts[key],1)
        self.assertEqual(health['valid_timeline_responses'],0)
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())
        self.assertNotIn('PRIVATE_TEST',json.dumps(health))

    def test_pagination_unknown_descendant_is_not_claimed_fully_known(self):
        result=diag.classify_error_path([ROOT,'page_info','PRIVATE_TEST'])
        self.assertEqual(result['category'],'timeline_pagination')
        self.assertTrue(result['unknown_below_required_field'])

    def test_overlong_and_malformed_paths_remain_bounded_and_unknown(self):
        for path in ([ROOT]+['PRIVATE_TEST']*32,[ROOT,'edges',True,'node'],
                     [ROOT,'edges',-1,'node'],[ROOT,{}]):
            result=diag.classify_error_path(path)
            self.assertIn(result['category'],('unknown','malformed_path'))
            self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_new_flags_are_sanitized_when_aggregated(self):
        pattern=diag.classify_error_path([ROOT,'edges',0,'node','caption','PRIVATE_TEST'])
        pattern.update(count=1,PRIVATE_TEST='PRIVATE_TEST')
        combined=diag.aggregate_diagnostics([{'graphql_error_diagnostics':{'path_classifications':[pattern]}}])
        self.assertTrue(combined['graphql_error_diagnostics']['path_classifications'][0]['unknown_below_required_field'])
        self.assertNotIn('PRIVATE_TEST',json.dumps(combined))

    def test_durable_log_preserves_new_scope_without_raw_path(self):
        from test_instagram_execution_log import ExecutionLogTests
        from app.parent_pipeline import execution_log as log
        fixture=ExecutionLogTests();fixture.setUp()
        try:
            data=payload(media())
            data['errors']=[{'message':'PRIVATE_TEST','path':[ROOT,'edges',0,'node','caption','PRIVATE_TEST']}]
            health,_,_=self.capture(data)
            def execute():
                log.begin('scraping')
                log.finish('scraping',dict(failed=1,scrape_health=health))
                return dict(status='failed')
            record=fixture.read(log.invoke(execute))
            item=record['stages']['scraping']['items'][0]
            pattern=item['graphql_error_diagnostics']['path_classifications'][0]
            self.assertEqual(pattern['category'],'timeline_caption')
            self.assertTrue(pattern['unknown_below_required_field'])
            self.assertEqual(item['graphql_error_timeline_correlation']['with_unknown_paths'],1)
            self.assertNotIn('PRIVATE_TEST',json.dumps(record))
        finally:
            fixture.doCleanups()
