"""Synthetic GraphQL and Playwright errors only; no live services or state."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch
from playwright.sync_api import Error as PlaywrightError

from test_instagram_event_extraction import serial
from app.instagram_scraper import sync_ins as scraper
from app.instagram_scraper.scrape_health import CAPTURE_ERROR_CATEGORIES
from test_instagram_scrape_health import response
import test_instagram_scrape_health as health_tests


class GraphqlDiagnosticsTests(unittest.TestCase):
    def capture(self, reply, **patches):
        health=scraper.new_capture_health();bodies=[];codes=set()
        handler,finalize=scraper._make_graphql_posts_capture(bodies,codes,health)
        with contextlib.ExitStack() as stack:
            for name,value in patches.items():stack.enter_context(patch.object(scraper,name,side_effect=value))
            handler(reply)
        self.assertEqual(sum(health['error_categories'].values()),health['capture_errors'])
        self.assertNotIn('PRIVATE_TEST',json.dumps(health))
        return health,bodies,codes

    def assert_category(self,reply,category,**patches):
        health,bodies,codes=self.capture(reply,**patches)
        self.assertEqual(health['error_categories'][category],1)
        self.assertEqual(health['capture_errors'],1)
        self.assertEqual(health['valid_timeline_responses'],0)
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')

    def test_body_unavailable(self):
        reply=response();reply.text.side_effect=RuntimeError('PRIVATE_TEST source URL cookie')
        self.assert_category(reply,'response_body_unavailable')

    def test_json_failure_including_unhandled_prefix(self):
        for text in ('PRIVATE_TEST not json','for (;;);{"data":{}}'):
            reply=response();reply.text.return_value=text
            self.assert_category(reply,'json_decoding_failure')

    def test_unexpected_json_structure(self):
        for text in ('[]','null','42','{"data":null}','{"data":[]}',
                     '{"data":{"timeline_connection":{"edges":null}}}'):
            reply=response();reply.text.return_value=text
            self.assert_category(reply,'unexpected_json_structure')

    def test_missing_timeline_edges_is_not_an_empty_timeline(self):
        for data in ({'data':{}},{'data':{'timeline_connection':{'page_info':{'has_next_page':False}}}}):
            self.assert_category(response(data),'missing_timeline_edges')

    def test_unsupported_nested_and_changed_node_schemas(self):
        for data in (
            {'data':{'user':{'edge_owner_to_timeline_media':{'edges':[]}}}},
            {'data':{'timeline_connection':{'items':[]}}},
            {'data':{'timeline_connection':{'nodes':[]}}},
            {'data':{'timeline_connection':{'edges':[{'node':{'shortcode':'PRIVATE_TEST'}}]}}},
            {'data':{'timeline_connection':{'edges':[{'node':None}]}}},
            {'data':{'timeline_connection':{'edges':[{'node':{'code':123}}]}}},
        ):
            self.assert_category(response(data),'unsupported_timeline_schema')

    def test_browser_context_closure_during_body_read(self):
        reply=response();reply.text.side_effect=PlaywrightError(
            'Response.text: Target page, context or browser has been closed PRIVATE_TEST')
        self.assert_category(reply,'browser_context_closed')

    def test_unexpected_processing_exception_is_contained(self):
        self.assert_category(response(),'response_processing_exception',
            _extract_timeline_nodes=RuntimeError('PRIVATE_TEST traceback'))
        self.assert_category(response(),'response_processing_exception',
            _collect_cdn_urls_excluding_account_images=ValueError('PRIVATE_TEST source'))

    def test_http_and_graphql_error_categories(self):
        self.assert_category(response(status=403),'http_error')
        self.assert_category(response({'data':None,'errors':[{'message':'PRIVATE_TEST'}]}),'graphql_error')

    def test_valid_empty_and_nonempty_timelines_are_distinct(self):
        empty,bodies,codes=self.capture(response())
        self.assertEqual(empty['valid_empty_timeline_responses'],1)
        self.assertEqual(empty['valid_timeline_responses'],1);self.assertEqual(len(bodies),1)
        self.assertIsNone(scraper.capture_failure(empty))
        full,bodies,codes=self.capture(response({'data':{'timeline_connection':{
            'edges':[{'node':{'code':'one'}}]}}}))
        self.assertEqual(full['valid_empty_timeline_responses'],0)
        self.assertEqual(full['valid_timeline_responses'],1);self.assertEqual(codes,{'one'})
        self.assertIsNone(scraper.capture_failure(full))

    def test_mixed_valid_and_malformed_edges_still_fail_closed(self):
        self.assert_category(response({'data':{'timeline_connection':{'edges':[
            {'node':{'code':'one'}},{'node':{'shortcode':'PRIVATE_TEST'}}]}}}),'unsupported_timeline_schema')

    def test_unrelated_graphql_is_ignored_but_cannot_verify_success(self):
        health,bodies,codes=self.capture(response({'data':{'inbox':{'edges':[]}}}))
        self.assertEqual(health['ignored_graphql_responses'],1)
        self.assertEqual(health['capture_errors'],0)
        self.assertEqual(scraper.capture_failure(health),'no_valid_timeline_response')

    def test_duplicate_empty_response_not_saved_twice(self):
        health=scraper.new_capture_health();bodies=[]
        handler,_=scraper._make_graphql_posts_capture(bodies,set(),health)
        handler(response());handler(response())
        self.assertEqual(health['valid_empty_timeline_responses'],2);self.assertEqual(len(bodies),1)

    def test_one_valid_response_does_not_override_a_capture_error(self):
        health=scraper.new_capture_health()
        handler,_=scraper._make_graphql_posts_capture([],set(),health)
        handler(response())
        handler(response({'data':{'timeline_connection':{}}}))
        self.assertEqual(health['valid_timeline_responses'],1)
        self.assertEqual(health['error_categories']['missing_timeline_edges'],1)
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')

    def test_failed_request_category_is_exposed_and_no_upsert_occurs(self):
        result,write=health_tests.ProfileHealthTests().scrape(request_failed=True)
        self.assertEqual(result['scrape_health']['error_categories']['request_failed'],1)
        write.assert_not_called()

    def test_schema_failure_is_exposed_and_no_upsert_occurs(self):
        result,write=health_tests.ProfileHealthTests().scrape(reply=response({'data':{'timeline_connection':{}}}))
        self.assertEqual(result['scrape_health']['error_categories']['missing_timeline_edges'],1)
        self.assertEqual(result['scrape_health']['reason'],'response_capture_failed')
        write.assert_not_called()

    def test_listener_cleanup_closure_is_safe_and_blocks_upsert(self):
        from unittest.mock import MagicMock
        page=MagicMock();page.url='https://www.instagram.com/dvcatucsd/'
        page.locator.return_value.count.return_value=0
        handlers={};page.on.side_effect=lambda event,handler:handlers.update({event:handler})
        def navigate(*args,**kwargs):handlers['response'](response());return Mock(status=200)
        page.goto.side_effect=navigate
        page.remove_listener.side_effect=PlaywrightError('Target page, context or browser has been closed PRIVATE_TEST')
        with patch.object(scraper,'known_shortcodes_for_profile',return_value=set()), \
             patch.object(scraper,'upsert_posts_for_profile') as upsert, \
             patch.object(scraper,'MAX_PROFILE_SCROLLS',0),contextlib.redirect_stdout(io.StringIO()):
            result=scraper._scroll_profile_capture_and_upsert(page,'dvcatucsd','https://www.instagram.com/dvcatucsd/')
        self.assertEqual(result['scrape_health']['status'],'failed')
        self.assertEqual(result['scrape_health']['error_categories']['browser_context_closed'],2)
        self.assertEqual(result['scrape_health']['capture_errors'],2)
        upsert.assert_not_called();self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_modes_share_capture_path_and_aggregate_error_categories(self):
        from unittest.mock import MagicMock
        results=[]
        for args,headless in (([],False),(['--headless'],True)):
            playwright=MagicMock()
            launch=playwright.return_value.__enter__.return_value.firefox.launch_persistent_context
            launch.return_value.pages=[MagicMock()]
            health=scraper.new_capture_health();scraper.capture_error(health,'json_decoding_failure')
            health.update(status='failed',reason='response_capture_failed')
            with patch.object(scraper,'TARGET_URLS',['dvcatucsd']), \
                 patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]), \
                 patch.object(scraper,'sync_playwright',playwright), \
                 patch.object(scraper,'SCREENSHOT_DIR',MagicMock()),patch.object(scraper,'PERSISTENT_PROFILE_DIR',MagicMock()), \
                 patch.object(scraper,'_scroll_profile_capture_and_upsert',return_value=dict(profile='dvcatucsd',
                     inserted=0,updated=0,unchanged=0,scrape_health=health)) as capture, \
                 contextlib.redirect_stdout(io.StringIO()):
                result=scraper.main(['--profile','dvcatucsd',*args])
            self.assertEqual(launch.call_args.kwargs['headless'],headless)
            self.assertEqual(capture.call_args.args[1:],('dvcatucsd','https://www.instagram.com/dvcatucsd/'))
            self.assertEqual(result['scrape_health']['error_categories']['json_decoding_failure'],1)
            self.assertEqual(result['scrape_health']['capture_errors'],1)
            results.append(result['scrape_health'])
        self.assertEqual(results[0],results[1])

    def test_durable_log_only_copies_allowlisted_numeric_categories(self):
        from test_instagram_execution_log import ExecutionLogTests
        from app.parent_pipeline import execution_log as log
        fixture=ExecutionLogTests();fixture.setUp()
        try:
            def execute():
                log.begin('scraping')
                log.finish('scraping',dict(failed=1,scrape_health=dict(error_categories={
                    'response_body_unavailable':2,'json_decoding_failure':3,'PRIVATE_TEST':10,
                    'graphql_error':'PRIVATE_TEST secret'},capture_errors=5)))
                return dict(status='failed',failed=1)
            data=fixture.read(log.invoke(execute))
            categories=data['stages']['scraping']['items'][0]['capture_error_categories']
            self.assertEqual(set(categories),set(CAPTURE_ERROR_CATEGORIES))
            self.assertEqual(categories['response_body_unavailable'],2)
            self.assertEqual(categories['json_decoding_failure'],3);self.assertEqual(categories['graphql_error'],0)
            self.assertNotIn('PRIVATE_TEST',json.dumps(data))
        finally:fixture.doCleanups()
