"""Privacy projections with synthetic payloads; no network, live DB or journals."""
import json
import contextlib
import io
import unittest
from unittest.mock import patch

from test_instagram_event_extraction import serial
from test_instagram_scrape_health import response
from app.instagram_scraper import sync_ins as scraper
from app.instagram_scraper import graphql_diagnostics as diag


class PrivacyDiagnosticsTests(unittest.TestCase):
    def capture(self,payload):
        health=scraper.new_capture_health();bodies=[];codes=set()
        handler,_=scraper._make_graphql_posts_capture(bodies,codes,health)
        handler(response(payload))
        self.assertNotIn('PRIVATE_TEST',json.dumps(health))
        return health,bodies,codes

    def test_numeric_allowlist_and_structural_fields(self):
        payload={'data':None,'errors':[{'code':190,'error_subcode':987654321,
            'message':'PRIVATE_TEST cookies','path':['PRIVATE_TEST account'],
            'locations':[{'PRIVATE_TEST':123}], 'summary':'PRIVATE_TEST',
            'extensions':{'code':1675030,'error_subcode':'PRIVATE_TEST',
                          'PRIVATE_TEST_token':'PRIVATE_TEST'}}]}
        health,bodies,codes=self.capture(payload)
        details=health['graphql_error_diagnostics']
        self.assertEqual(details['responses'],1);self.assertEqual(details['error_objects'],1)
        self.assertEqual(details['code_counts']['190'],1)
        self.assertEqual(details['code_counts']['1675030'],1)
        self.assertNotIn('987654321',json.dumps(details))
        pattern=details['structures'][0]
        for key in ('has_message','has_path','has_locations','has_summary','has_extensions','has_code',
                    'has_error_subcode','has_extensions_code','has_extensions_error_subcode'):
            self.assertIs(pattern[key],True)
        self.assertEqual(health['error_categories']['graphql_error'],1)
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())

    def test_unknown_numeric_and_non_integer_codes_are_withheld(self):
        for code in (987654321,'190','PRIVATE_TEST',True,None,3.14,{'PRIVATE_TEST':'PRIVATE_TEST'}):
            health,_,_=self.capture({'errors':[{'code':code,'message':'PRIVATE_TEST'}]})
            details=health['graphql_error_diagnostics'];pattern=details['structures'][0]
            self.assertEqual(sum(details['code_counts'].values()),0)
            self.assertEqual(pattern['has_unallowlisted_numeric_code'],type(code) is int)
            self.assertEqual(pattern['has_non_numeric_code'],type(code) is not int)
            self.assertNotIn('987654321',json.dumps(details))

    def test_only_known_code_fields_are_considered_and_duplicate_codes_coalesce(self):
        health,_,_=self.capture({'errors':[{'code':190,'error_code':190,'api_error_code':190,
            'id':123456789,'PRIVATE_TEST':190,'extensions':{'code':190,'id':123456789}}]})
        self.assertEqual(health['graphql_error_diagnostics']['code_counts']['190'],1)
        self.assertNotIn('123456789',json.dumps(health))

    def test_error_containers_and_nonobject_items_are_fixed_enums(self):
        for errors,container in (({'message':'PRIVATE_TEST'},'object'),(['PRIVATE_TEST',False,7],'list'),
                                 ('PRIVATE_TEST','other')):
            health,_,_=self.capture({'errors':errors})
            patterns=health['graphql_error_diagnostics']['structures']
            self.assertTrue(all(p['container']==container for p in patterns))
            self.assertTrue(all(p['item_kind'] in ('object','other') for p in patterns))

    def test_large_error_lists_are_bounded_and_report_omissions(self):
        health,_,_=self.capture({'errors':[{'message':'PRIVATE_TEST'}]*130})
        details=health['graphql_error_diagnostics']
        self.assertEqual(details['error_objects'],128);self.assertEqual(details['omitted_error_items'],2)
        self.assertEqual(details['structures'][0]['count'],128)

    def test_nested_timeline_and_media_indicators_without_parser_changes(self):
        payload={'data':{'user':{'timeline_connection':{'edges':[{'node':{'media':{
            'shortcode':'PRIVATE_TEST','id':'PRIVATE_TEST','caption':'PRIVATE_TEST'}}}],
            'page_info':{'end_cursor':'PRIVATE_TEST','has_next_page':True}}}}}
        self.assertEqual(scraper._extract_timeline_nodes(payload),[])
        health,bodies,codes=self.capture(payload)
        self.assertEqual(health['error_categories']['unsupported_timeline_schema'],1)
        shape=health['unsupported_timeline_shapes'][0]
        for key in ('data_present','data_is_object','edges_present','has_list_edges','nodes_nested',
                    'node_has_shortcode','node_has_id','pagination_present'):
            self.assertIs(shape[key],True)
        self.assertFalse(shape['node_has_code']);self.assertEqual(shape['shape_kind'],'nested_edges')
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())

    def test_shortcodes_in_top_level_edges_remain_unsupported(self):
        payload={'data':{'timeline_connection':{'edges':[{'node':{'shortcode':'PRIVATE_TEST','id':'PRIVATE_TEST'}}]}}}
        health,bodies,_=self.capture(payload)
        shape=health['unsupported_timeline_shapes'][0]
        self.assertEqual(shape['shape_kind'],'top_level_edges')
        self.assertFalse(shape['nodes_nested']);self.assertTrue(shape['node_has_shortcode'])
        self.assertFalse(shape['node_has_code'])
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed');self.assertEqual(bodies,[])

    def test_alternate_nodes_and_pagination_values_never_escape(self):
        payload={'data':{'timeline_connection':{'items':[{'id':'PRIVATE_TEST','code':'PRIVATE_TEST'}],
            'next_max_id':'PRIVATE_TEST signed cursor'}}}
        health,_,_=self.capture(payload);shape=health['unsupported_timeline_shapes'][0]
        self.assertEqual(shape['shape_kind'],'alternate_nodes')
        self.assertTrue(shape['nodes_nested']);self.assertTrue(shape['pagination_present'])
        self.assertTrue(shape['node_has_code']);self.assertTrue(shape['node_has_id'])

    def test_supported_nonempty_and_empty_shapes_keep_acceptance_rules(self):
        for edges in ([],[{'node':{'code':'one','id':'PRIVATE_TEST'}}]):
            payload={'data':{'timeline_connection':{'edges':edges,'page_info':{'end_cursor':'PRIVATE_TEST'}}}}
            shape=diag.schema_shape(payload)
            self.assertEqual(shape['shape_kind'],'top_level_edges')
            self.assertFalse(shape['nodes_nested']);self.assertTrue(shape['edges_present'])
            self.assertEqual(shape['node_has_code'],bool(edges))
            health,_,_=self.capture(payload)
            self.assertEqual(health['valid_timeline_responses'],1)
            self.assertEqual(health['valid_empty_timeline_responses'],int(not edges))
            self.assertEqual(health['unsupported_timeline_shapes'],[])
            self.assertIsNone(scraper.capture_failure(health))

    def test_null_missing_data_and_nonlist_edges_have_boolean_indicators(self):
        for payload,present in (({},False),({'data':None},True)):
            shape=diag.schema_shape(payload)
            self.assertEqual(shape['data_present'],present);self.assertFalse(shape['data_is_object'])
            self.assertFalse(shape['edges_present'])
        shape=diag.schema_shape({'data':{'timeline_connection':{'edges':None}}})
        self.assertTrue(shape['edges_present']);self.assertTrue(shape['has_non_list_edges'])
        self.assertFalse(shape['has_list_edges'])

    def test_camel_case_pagination_and_user_metadata_do_not_leak_values(self):
        shape=diag.schema_shape({'data':{'timeline_connection':{'edges':[{'node':{
            'code':'PRIVATE_TEST','user':{'id':'PRIVATE_TEST'}}}],
            'pageInfo':{'endCursor':'PRIVATE_TEST'}}}})
        self.assertTrue(shape['pagination_present']);self.assertTrue(shape['node_has_code'])
        self.assertFalse(shape['node_has_id'])  # A nested user's ID is not a node ID.
        self.assertNotIn('PRIVATE_TEST',json.dumps(shape))

    def test_deep_shape_inspection_is_bounded(self):
        root={'items':[{'code':'PRIVATE_TEST'}]}
        for _ in range(30):root={'PRIVATE_TEST':root}
        shape=diag.schema_shape({'data':root})
        self.assertTrue(shape['inspection_truncated'])
        self.assertNotIn('PRIVATE_TEST',json.dumps(shape))

    def test_aggregates_coalesce_patterns_and_whitelist_again(self):
        error,_,_=self.capture({'errors':[{'code':190,'message':'PRIVATE_TEST'}]})
        schema,_,_=self.capture({'data':{'timeline_connection':{'items':[{'id':'PRIVATE_TEST'}]}}})
        aggregated=diag.aggregate_diagnostics([error,error,schema,schema])
        self.assertEqual(aggregated['graphql_error_diagnostics']['responses'],2)
        self.assertEqual(aggregated['graphql_error_diagnostics']['code_counts']['190'],2)
        self.assertEqual(aggregated['graphql_error_diagnostics']['structures'][0]['count'],2)
        self.assertEqual(aggregated['unsupported_timeline_shapes'][0]['count'],2)
        malicious={'graphql_error_diagnostics':dict(responses='PRIVATE_TEST',code_counts={
            '190':'PRIVATE_TEST','987654321':42},structures=[dict(count=1,has_message='PRIVATE_TEST',
            container='PRIVATE_TEST',item_kind='PRIVATE_TEST',PRIVATE_TEST='PRIVATE_TEST')]),
            'unsupported_timeline_shapes':[dict(count=1,data_present='PRIVATE_TEST',shape_kind='PRIVATE_TEST',
                                              PRIVATE_TEST='PRIVATE_TEST')]}
        clean=diag.aggregate_diagnostics([malicious]);self.assertNotIn('PRIVATE_TEST',json.dumps(clean))
        self.assertNotIn('987654321',json.dumps(clean))
        self.assertEqual(clean['graphql_error_diagnostics']['structures'][0]['container'],'other')

    def test_scraper_profile_aggregation_and_log_projection(self):
        from unittest.mock import MagicMock
        from test_instagram_execution_log import ExecutionLogTests
        error,_,_=self.capture({'errors':[{'code':190,'message':'PRIVATE_TEST'}]})
        unsupported,_,_=self.capture({'data':{'timeline_connection':{'items':[{'shortcode':'PRIVATE_TEST'}]}}})
        profiles=[dict(profile='dvcatucsd',scrape_health=dict(error,status='failed',reason='response_capture_failed')),
                  dict(profile='acm.at.ucsd',scrape_health=dict(unsupported,status='failed',reason='response_capture_failed'))]
        playwright=MagicMock();playwright.return_value.__enter__.return_value.firefox.launch_persistent_context.return_value.pages=[MagicMock()]
        with patch.object(scraper,'TARGET_URLS',['dvcatucsd','acm.at.ucsd']), \
             patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]), \
             patch.object(scraper,'sync_playwright',playwright),patch.object(scraper,'SCREENSHOT_DIR',MagicMock()), \
             patch.object(scraper,'PERSISTENT_PROFILE_DIR',MagicMock()), \
             patch.object(scraper,'_scroll_profile_capture_and_upsert',side_effect=profiles), \
             contextlib.redirect_stdout(io.StringIO()):
            result=scraper.main([])
        root=result['scrape_health']
        self.assertEqual(root['graphql_error_diagnostics']['code_counts']['190'],1)
        self.assertEqual(root['unsupported_timeline_shapes'][0]['count'],1)
        fixture=ExecutionLogTests();fixture.setUp()
        try:
            from app.parent_pipeline import execution_log as log
            def execute():log.begin('scraping');log.finish('scraping',result);return dict(status='failed',failed=1)
            data=fixture.read(log.invoke(execute))
            item=data['stages']['scraping']['items'][0]
            self.assertEqual(item['graphql_error_diagnostics'],root['graphql_error_diagnostics'])
            self.assertEqual(item['unsupported_timeline_shapes'],root['unsupported_timeline_shapes'])
            self.assertNotIn('PRIVATE_TEST',json.dumps(data))
        finally:fixture.doCleanups()
