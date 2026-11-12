"""Synthetic candidates, not verified Instagram schemas; diagnostics never enable parsing."""
import json
import unittest

from test_instagram_event_extraction import serial
from test_instagram_scrape_health import response
from app.instagram_scraper import sync_ins as scraper, graphql_diagnostics as diag

ROOT='xdt_api__v1__feed__user_timeline_graphql_connection'


def payload(node):
    return {'data':{ROOT:{'edges':[{'node':node}],'page_info':{'end_cursor':'PRIVATE_TEST'}}}}


class AttributionTests(unittest.TestCase):
    def capture(self,data):
        health=scraper.new_capture_health();bodies=[];codes=set()
        handler,_=scraper._make_graphql_posts_capture(bodies,codes,health)
        handler(response(data))
        self.assertNotIn('PRIVATE_TEST',json.dumps(health))
        return health,bodies,codes

    def test_confirmed_node_media_is_accepted(self):
        media={'code':'PRIVATE_TEST','id':'PRIVATE_TEST','pk':'PRIVATE_TEST',
            'taken_at':123456789,'caption':{'text':'PRIVATE_TEST'},'user':{'username':'PRIVATE_TEST'},
            'image_versions2':{'candidates':[{'url':'https://test.cdninstagram.com/test.jpg'}]}}
        data=payload({'media':media})
        health,bodies,codes=self.capture(data)
        self.assertEqual(scraper._extract_timeline_nodes(data),[media])
        self.assertEqual(health['valid_timeline_responses'],1)
        self.assertEqual(len(bodies),1);self.assertEqual(codes,{'PRIVATE_TEST'})
        self.assertEqual(health['capture_errors'],0)

    def test_other_fixed_wrapper_positions_are_diagnosed_without_extraction(self):
        for node,expected in (({'media_or_ad':{'code':'PRIVATE_TEST'}},'node_media_or_ad'),
                              ({'media_or_ad':{'media':{'code':'PRIVATE_TEST'}}},'node_media_or_ad_media')):
            health,_,_=self.capture(payload(node))
            pattern=next(p for p in health['timeline_wrapper_diagnostics']['patterns'] if p['wrapper_kind']==expected)
            self.assertTrue(pattern['code_is_nonempty_string'])
            self.assertEqual(health['valid_timeline_responses'],0)

    def test_arbitrary_recursive_nodes_are_not_treated_as_media_candidates(self):
        health,bodies,_=self.capture(payload({'PRIVATE_TEST_wrapper':{'code':'PRIVATE_TEST'}}))
        patterns=health['timeline_wrapper_diagnostics']['patterns']
        self.assertEqual(len(patterns),1);self.assertEqual(patterns[0]['wrapper_kind'],'flat_node')
        self.assertFalse(patterns[0]['has_code']);self.assertEqual(bodies,[])

    def test_under_user_connection_is_distinct_and_still_unsupported(self):
        data={'data':{'user':{'edge_owner_to_timeline_media':{'edges':[{'node':{'code':'PRIVATE_TEST'}}]}}}}
        health,_,_=self.capture(data)
        pattern=health['timeline_wrapper_diagnostics']['patterns'][0]
        self.assertEqual(pattern['connection_placement'],'under_user')
        self.assertTrue(pattern['code_is_nonempty_string'])
        self.assertEqual(scraper._extract_timeline_nodes(data),[])

    def test_malformed_wrappers_have_fixed_indicators(self):
        for node in ({'media':None},{'media':[]},{'media':{'code':17}},{'media':{'code':''}}):
            health,_,_=self.capture(payload(node))
            pattern=next(p for p in health['timeline_wrapper_diagnostics']['patterns'] if p['wrapper_kind']=='node_media')
            self.assertFalse(pattern['code_is_nonempty_string'])
            self.assertEqual(health['valid_timeline_responses'],0)
        for edges,expected in (([None],'non_object_edge'),([{}],'missing_node'),([{'node':None}],'non_object_node'),
                               (None,'non_list_edges'),([],'empty_edges')):
            details=diag.wrapper_diagnostics({'data':{ROOT:{'edges':edges}}})
            self.assertEqual(details['patterns'][0]['wrapper_kind'],expected)

    def test_wrapper_inspection_is_bounded(self):
        data={'data':{ROOT:{'edges':[{'node':{'media':{'code':'PRIVATE_TEST'}}}]*140}}}
        details=diag.wrapper_diagnostics(data)
        self.assertEqual(details['inspected_edges'],128);self.assertEqual(details['omitted_items'],12)
        self.assertNotIn('PRIVATE_TEST',json.dumps(details))

    def test_flat_and_empty_extraction_stays_unchanged(self):
        for data in (payload({'code':'one'}),{'data':{ROOT:{'edges':[]}}}):
            health,_,_=self.capture(data)
            self.assertEqual(health['valid_timeline_responses'],1)
            self.assertEqual(health['timeline_wrapper_diagnostics']['responses'],0)
            self.assertIsNone(scraper.capture_failure(health))

    def test_timeline_critical_path_categories(self):
        for suffix,category in ((['code'],'timeline_identity'),(['taken_at'],'timeline_time'),
            (['image_versions2','candidates',0,'url'],'timeline_media'),(['caption','text'],'timeline_caption'),
            (['user','username'],'timeline_organizer')):
            for wrapper in ([],['media'],['media_or_ad','media']):
                result=diag.classify_error_path([ROOT,'edges',17,'node',*wrapper,*suffix])
                self.assertEqual(result['category'],category);self.assertTrue(result['known_timeline_root'])
                self.assertTrue(result['has_indices'])
        self.assertEqual(diag.classify_error_path([ROOT,'page_info','end_cursor'])['category'],'timeline_pagination')
        self.assertEqual(diag.classify_error_path([ROOT])['category'],'timeline_connection')
        self.assertEqual(diag.classify_error_path(['user','edge_owner_to_timeline_media','edges',0,'node','code'])['category'],
                         'timeline_identity')

    def test_optional_paths_are_candidates_not_permission_to_ignore_errors(self):
        path=[ROOT,'edges',0,'node','viewer_has_liked']
        self.assertEqual(diag.classify_error_path(path)['category'],'optional_node_field_candidate')
        self.assertEqual(diag.classify_error_path(['inbox'])['category'],'optional_root_candidate')
        data=payload({'code':'one'});data['errors']=[{'message':'PRIVATE_TEST','path':path}]
        health,bodies,_=self.capture(data)
        self.assertEqual(health['error_categories']['graphql_error'],1)
        self.assertEqual(health['graphql_error_diagnostics']['path_classifications'][0]['category'],
                         'optional_node_field_candidate')
        self.assertEqual(health['valid_timeline_responses'],0);self.assertEqual(bodies,[])
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')
        # Comment metadata IS read by the scraper, so it is not silently optional.
        self.assertEqual(diag.classify_error_path([ROOT,'edges',0,'node','comment_count'])['category'],
                         'timeline_comments')

    def test_unknown_or_mixed_paths_do_not_gain_optional_attribution(self):
        for path in ([ROOT,'edges',0,'node','PRIVATE_TEST'],['inbox','PRIVATE_TEST'],['PRIVATE_TEST','edges',123456789]):
            result=diag.classify_error_path(path)
            self.assertIn(result['category'],('unknown','timeline_unknown_field'))
            self.assertTrue(result['contains_unknown_segments'])
            self.assertNotIn('PRIVATE_TEST',json.dumps(result));self.assertNotIn('123456789',json.dumps(result))

    def test_missing_malformed_and_long_paths_are_bounded(self):
        self.assertEqual(diag.classify_error_path(None,present=False)['category'],'missing_path')
        for path in (None,[],{},'PRIVATE_TEST',[ROOT,True],[ROOT,-1]):
            self.assertEqual(diag.classify_error_path(path)['category'],'malformed_path')
        long=diag.classify_error_path([ROOT]+['PRIVATE_TEST']*40)
        self.assertTrue(long['inspection_truncated']);self.assertEqual(long['category'],'unknown')
        self.assertNotIn('PRIVATE_TEST',json.dumps(long))

    def test_path_and_wrapper_patterns_are_aggregated_and_sanitized(self):
        health,_,_=self.capture(payload({'media':{'code':'PRIVATE_TEST'}}))
        handler,_=scraper._make_graphql_posts_capture([],set(),health)
        handler(response({'errors':[{'message':'PRIVATE_TEST','path':[ROOT,'edges',0,'node','code']}]}))
        combined=diag.aggregate_diagnostics([health,health])
        self.assertEqual(combined['timeline_wrapper_diagnostics']['responses'],4)
        self.assertEqual(combined['graphql_error_diagnostics']['path_classifications'][0]['count'],2)
        self.assertNotIn('PRIVATE_TEST',json.dumps(combined))
        malicious={'graphql_error_diagnostics':{'path_classifications':[dict(count=1,category='PRIVATE_TEST',
            has_indices='PRIVATE_TEST',PRIVATE_TEST='PRIVATE_TEST')]},'timeline_wrapper_diagnostics':{
            'patterns':[dict(count=1,wrapper_kind='PRIVATE_TEST',connection_placement='PRIVATE_TEST',
                             root_kind='PRIVATE_TEST',has_code='PRIVATE_TEST',PRIVATE_TEST='PRIVATE_TEST')]}}
        clean=diag.aggregate_diagnostics([malicious])
        self.assertNotIn('PRIVATE_TEST',json.dumps(clean))
        self.assertEqual(clean['graphql_error_diagnostics']['path_classifications'][0]['category'],'unknown')

    def test_durable_log_retains_only_fixed_diagnostics(self):
        from test_instagram_execution_log import ExecutionLogTests
        from app.parent_pipeline import execution_log as log
        fixture=ExecutionLogTests();fixture.setUp()
        try:
            health,_,_=self.capture(payload({'media':{'code':'PRIVATE_TEST'}}))
            handler,_=scraper._make_graphql_posts_capture([],set(),health)
            handler(response({'errors':[{'message':'PRIVATE_TEST','path':[ROOT,'edges',0,'node','caption']}]}))
            def execute():log.begin('scraping');log.finish('scraping',dict(failed=1,scrape_health=health));return dict(status='failed')
            data=fixture.read(log.invoke(execute));item=data['stages']['scraping']['items'][0]
            self.assertEqual(item['timeline_wrapper_diagnostics'],health['timeline_wrapper_diagnostics'])
            self.assertEqual(item['graphql_error_diagnostics']['path_classifications'],
                             health['graphql_error_diagnostics']['path_classifications'])
            self.assertNotIn('PRIVATE_TEST',json.dumps(data))
        finally:fixture.doCleanups()
