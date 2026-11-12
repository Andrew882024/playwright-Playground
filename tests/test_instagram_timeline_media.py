"""Offline regression coverage for the confirmed node.media boundary."""
import json
import unittest

from test_instagram_event_extraction import serial
from test_instagram_scrape_health import response
from test_instagram_graphql_attribution import payload, ROOT
from app.instagram_scraper import sync_ins as scraper, graphql_diagnostics as diag
from app.instagram_scraper.timeline_media import resolve_timeline_media


def media():
    return dict(code='safe_code',id='123',pk=123,taken_at=1793808000,
                caption={'text':'PRIVATE_CAPTION'},user={'username':'PRIVATE_ACCOUNT'},
                image_versions2={'candidates':[{'url':'https://test.cdninstagram.com/private.jpg'}]})


class TimelineMediaTests(unittest.TestCase):
    def capture(self,data):
        health=scraper.new_capture_health();bodies=[];codes=set()
        handler,_=scraper._make_graphql_posts_capture(bodies,codes,health)
        handler(response(data))
        return health,bodies,codes

    def test_nested_and_flat_share_validation(self):
        for node in (media(),{'media':media()}):
            data=payload(node)
            health,bodies,codes=self.capture(data)
            self.assertEqual(scraper._extract_timeline_nodes(data),[media()])
            self.assertEqual(health['valid_timeline_responses'],1)
            self.assertEqual(codes,{'safe_code'});self.assertEqual(len(bodies),1)

    def test_sparse_legacy_flat_supported(self):
        self.assertEqual(resolve_timeline_media({'node':{'code':'legacy'}}),({'code':'legacy'},'flat_media'))

    def test_nested_required_fields_and_types(self):
        for key in ('code','id','pk','taken_at','caption','user','image_versions2'):
            value=media();value[key]=[]
            with self.subTest(key=key):
                health,bodies,_=self.capture(payload({'media':value}))
                self.assertEqual(health['error_categories']['unsupported_timeline_schema'],1)
                self.assertEqual(bodies,[])
        for key in ('taken_at','caption','user','image_versions2'):
            value=media();del value[key]
            self.assertIsNone(resolve_timeline_media({'node':{'media':value}})[0])
        for value in (True,-1,0,1e30,10**100):
            node=media();node['taken_at']=value
            self.assertIsNone(resolve_timeline_media({'node':{'media':node}})[0])

    def test_nested_image_and_caption_inner_types_are_validated(self):
        for field,value in (('caption',{'text':17}),('user',{'username':False}),
                            ('image_versions2',{'candidates':None}),
                            ('image_versions2',{'candidates':[]}),
                            ('image_versions2',{'candidates':[None]}),
                            ('image_versions2',{'candidates':[{'url':True}]})):
            node=media();node[field]=value
            self.assertIsNone(resolve_timeline_media({'node':{'media':node}})[0])

    def test_caption_null_is_valid_but_null_media_is_not_a_valid_empty_timeline(self):
        node=media();node['caption']=None
        self.assertIsNotNone(resolve_timeline_media({'node':{'media':node}})[0])
        for node in (None,{'media':None}):
            self.assertEqual(resolve_timeline_media({'node':node})[1],'null_placeholder')
            health,bodies,_=self.capture(payload(node))
            self.assertEqual(health['valid_empty_timeline_responses'],0)
            self.assertEqual(health['valid_timeline_responses'],0);self.assertEqual(bodies,[])

    def test_mixed_valid_and_unresolved_nodes_fail_closed(self):
        data=payload({'media':media()})
        data['data'][ROOT]['edges'].append({'node':{'media':None}})
        health,bodies,codes=self.capture(data)
        self.assertEqual(health['valid_timeline_responses'],0)
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())

    def test_null_list_and_scalar_have_separate_private_indicators(self):
        for value,flag in ((None,'candidate_is_null'),([],'candidate_is_list'),
                           ('PRIVATE_VALUE','candidate_is_scalar'),(42,'candidate_is_scalar')):
            details=diag.wrapper_diagnostics(payload({'media':value}))
            pattern=next(p for p in details['patterns'] if p['wrapper_kind']=='node_media')
            self.assertTrue(pattern[flag]);self.assertFalse(pattern['candidate_is_object'])
            self.assertNotIn('PRIVATE_VALUE',json.dumps(details))

    def test_outer_pin_flag_survives_unwrapping(self):
        result,kind=resolve_timeline_media({'node':{'media':media(),'is_pinned':True}})
        self.assertTrue(result['is_pinned']);self.assertEqual(kind,'nested_media')
        self.assertNotIn('is_pinned',media())

    def test_unconfirmed_wrappers_stay_unsupported(self):
        for node in ({'other':media()},{'media_or_ad':media()}, {'media':{'other':media()}}):
            self.assertIsNone(resolve_timeline_media({'node':node})[0])

    def test_error_response_shape_is_inspected_but_never_accepted(self):
        data=payload({'media':media()})
        data['errors']=[{'message':'PRIVATE_ERROR','path':[ROOT,'edges',0,'node','PRIVATE_FIELD']}]
        health,bodies,codes=self.capture(data)
        counts=health['graphql_error_timeline_correlation']
        for key in ('responses','with_recognized_media','with_nested_media','with_unknown_paths'):
            self.assertEqual(counts[key],1)
        self.assertEqual(health['timeline_wrapper_diagnostics']['inspected_edges'],1)
        self.assertEqual(health['valid_timeline_responses'],0)
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())
        for secret in ('PRIVATE_ERROR','PRIVATE_FIELD','PRIVATE_CAPTION','PRIVATE_ACCOUNT','private.jpg'):
            self.assertNotIn(secret,json.dumps(health))

    def test_correlation_does_not_combine_unrelated_responses(self):
        health,bodies,_=self.capture({'errors':[{'path':[ROOT,'edges',0,'node','code']}]})
        handler,_=scraper._make_graphql_posts_capture(bodies,set(),health)
        handler(response(payload({'media':media()})))
        counts=health['graphql_error_timeline_correlation']
        self.assertEqual(counts['with_recognized_media'],0)
        self.assertEqual(counts['with_critical_paths'],1)
        self.assertEqual(health['valid_timeline_responses'],1)
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')

    def test_correlation_aggregated_and_allowlisted(self):
        data=payload({'media':media()});data['errors']=[{'path':['inbox']}]
        health,_,_=self.capture(data)
        counts=diag.aggregate_diagnostics([health,health])['graphql_error_timeline_correlation']
        self.assertEqual(counts['with_nested_media'],2)
        self.assertEqual(counts['with_optional_candidate_paths'],2)
        self.assertNotIn('PRIVATE',json.dumps(diag.aggregate_diagnostics([
            {'graphql_error_timeline_correlation':{'PRIVATE':123,'responses':'PRIVATE'}}])))

    def test_durable_log_retains_correlation(self):
        from test_instagram_execution_log import ExecutionLogTests
        from app.parent_pipeline import execution_log as log
        fixture=ExecutionLogTests();fixture.setUp()
        try:
            data=payload({'media':media()});data['errors']=[{'path':[ROOT]}]
            health,_,_=self.capture(data)
            def execute():
                log.begin('scraping');log.finish('scraping',dict(failed=1,scrape_health=health))
                return dict(status='failed')
            record=fixture.read(log.invoke(execute))
            self.assertEqual(record['stages']['scraping']['items'][0]['graphql_error_timeline_correlation'],
                             health['graphql_error_timeline_correlation'])
        finally:
            fixture.doCleanups()
