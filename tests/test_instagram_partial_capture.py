"""Offline partial diagnostics: visible fields never imply production eligibility."""
import contextlib
import copy
from datetime import datetime,timezone
import io
import json
import unittest
from unittest.mock import patch

from test_instagram_event_extraction import serial
from test_instagram_scrape_health import response
from test_instagram_graphql_attribution import ROOT,payload
from app.instagram_scraper import sync_ins as scraper
from app.instagram_scraper.partial_capture import PartialCapture
import test_instagram_schema_capture as capture_tests
import diagnose_instagram_graphql_schema as cli

NOW=datetime(2026,11,6,tzinfo=timezone.utc)


def media():
    return dict(code='PRIVATE_CODE',id='12345',pk=12345,media_type=1,
                taken_at=int(datetime(2026,11,3,tzinfo=timezone.utc).timestamp()),
                caption={'text':'PRIVATE_CAPTION'},user={'username':'dvcatucsd'},
                image_versions2={'candidates':[{'url':'https://test.cdninstagram.com/private.jpg?sig=PRIVATE'}]})


class PartialCaptureTests(unittest.TestCase):
    def session(self):return PartialCapture('dvcatucsd',now=NOW)

    def observe(self,media_node=None,*,errors=False):
        node=media() if media_node is None else media_node
        data=payload(node)
        if errors:data['errors']=[{'message':'PRIVATE_ERROR','path':[ROOT,'edges',0,'node','PRIVATE_FIELD']}]
        session=self.session();session.observe(data,'graphql_error' if errors else 'valid_timeline')
        summary=session.summary()
        for secret in ('PRIVATE_CODE','PRIVATE_CAPTION','PRIVATE_ERROR','PRIVATE_FIELD','private.jpg','dvcatucsd'):
            self.assertNotIn(secret,json.dumps(summary))
        return session

    def test_complete_candidate_is_only_partial_unverified(self):
        summary=self.observe().summary()
        self.assertEqual(summary['status'],'partial_unverified');self.assertFalse(summary['production_eligible'])
        self.assertEqual(summary['counts']['fully_valid_candidates'],1)
        self.assertEqual(summary['counts']['unique_fully_valid_candidates'],1)
        self.assertEqual(summary['counts']['unique_post_identities'],1)

    def test_missing_and_invalid_caption(self):
        node=media();del node['caption']
        self.assertEqual(self.observe(node).reasons['missing_caption'],1)
        for caption in (None,'PRIVATE_CAPTION',{'text':17},{}):
            node=media();node['caption']=caption
            session=self.observe(node)
            self.assertEqual(session.reasons['invalid_caption'],1)
            self.assertEqual(session.counts['fully_valid_candidates'],0)
        node=media();node['caption']={'text':''}
        self.assertEqual(self.observe(node).counts['fully_valid_candidates'],1)

    def test_missing_image_and_video_url_not_counted_as_poster(self):
        node=media();del node['image_versions2'];node['video_url']='https://test.cdninstagram.com/video.mp4'
        self.assertEqual(self.observe(node).reasons['missing_image'],1)

    def test_image_urls_require_supported_host_https_and_no_credentials(self):
        for url in ('http://test.cdninstagram.com/a.jpg','https://evil.example/cdninstagram/a.jpg',
                    'https://user:password@test.cdninstagram.com/a.jpg',
                    'https://test.cdninstagram.com:444/a.jpg','data:image/png;base64,PRIVATE'):
            node=media();node['image_versions2']['candidates'][0]['url']=url
            self.assertEqual(self.observe(node).reasons['missing_image'],1)

    def test_invalid_timestamp(self):
        for stamp in (None,True,0,-1,'1793664000',10**100,int(NOW.timestamp())+3600):
            node=media();node['taken_at']=stamp
            self.assertEqual(self.observe(node).reasons['invalid_timestamp'],1)

    def test_source_account_must_match_payload_not_visited_page(self):
        node=media();node['user']={'username':'other_account'}
        self.assertEqual(self.observe(node).reasons['source_account_mismatch'],1)
        node=media();del node['user']
        self.assertEqual(self.observe(node).reasons['source_account_missing'],1)

    def test_identity_is_not_invented_from_pk(self):
        for code in ('',' ',None,17,'https://example.com','code with spaces','x'*33):
            node=media();node['code']=code
            self.assertEqual(self.observe(node).reasons['invalid_identity'],1)

    def test_duplicate_identity_is_counted_without_merging_observations(self):
        session=self.session();data=payload(media())
        data['data'][ROOT]['edges'].append({'node':media()})
        session.observe(data,'valid_timeline');session.observe(payload(media()),'graphql_error')
        self.assertEqual(session.counts['candidate_media_nodes'],3)
        self.assertEqual(session.counts['unique_post_identities'],1)
        self.assertEqual(session.counts['duplicate_candidates'],2)
        self.assertEqual(session.counts['fully_valid_candidates'],3)
        self.assertEqual(session.counts['unique_fully_valid_candidates'],1)
        session=self.session();first=media();del first['caption'];second=media();del second['image_versions2']
        session.observe(payload(first),'graphql_error');session.observe(payload(second),'graphql_error')
        self.assertEqual(session.counts['fully_valid_candidates'],0)
        self.assertEqual(session.counts['incomplete_candidates'],2)

    def carousel(self):
        node=media();node['media_type']=8;node['carousel_media_count']=2
        node['carousel_media']=[dict(pk=i,image_versions2=copy.deepcopy(node['image_versions2'])) for i in (11,12)]
        return node

    def test_complete_carousel_requires_count_distinct_children_and_images(self):
        self.assertEqual(self.observe(self.carousel()).counts['fully_valid_candidates'],1)
        node=self.carousel();del node['carousel_media_count']
        self.assertEqual(self.observe(node).reasons['carousel_count_unverified'],1)
        for alteration in ('missing_child','missing_child_image','duplicate_child','non_object'):
            node=self.carousel()
            if alteration=='missing_child':node['carousel_media'].pop()
            if alteration=='missing_child_image':del node['carousel_media'][0]['image_versions2']
            if alteration=='duplicate_child':node['carousel_media'][0]['pk']=12
            if alteration=='non_object':node['carousel_media'][0]=None
            self.assertEqual(self.observe(node).reasons['carousel_incomplete'],1)

    def test_legacy_carousel_and_pagination(self):
        node=self.carousel();children=node.pop('carousel_media');node.pop('carousel_media_count')
        node['edge_sidecar_to_children']={'count':2,'edges':[{'node':child} for child in children],
                                         'page_info':{'has_next_page':False}}
        self.assertEqual(self.observe(node).counts['fully_valid_candidates'],1)
        node['edge_sidecar_to_children']['page_info']['has_next_page']=True
        self.assertEqual(self.observe(node).reasons['carousel_incomplete'],1)

    def test_null_and_malformed_slots_do_not_become_candidates(self):
        data=payload({'media':media()})
        data['data'][ROOT]['edges'] += [{'node':{'media':None}}]*3+[
            {'node':{'media':[]}},{'node':{'media':'PRIVATE'}},
            {'node':{'media_or_ad':media()}}]
        session=self.session();session.observe(data,'unsupported_timeline_schema')
        self.assertEqual(session.counts['candidate_media_nodes'],1)
        self.assertEqual(session.counts['fully_valid_candidates'],1)
        self.assertEqual(session.counts['null_media_slots'],3)
        self.assertEqual(session.counts['malformed_media_slots'],2)
        self.assertEqual(session.counts['unsupported_media_slots'],1)

    def test_graphql_error_with_valid_media_stays_production_rejected(self):
        session=self.session();health=scraper.new_capture_health();bodies=[];codes=set()
        handler,_=scraper._make_graphql_posts_capture(bodies,codes,health,diagnostic_payload=session.observe)
        data=payload(media());data['errors']=[{'message':'PRIVATE_ERROR'}]
        before_responses=health['graphql_responses'];before_errors=health['capture_errors']
        handler(response(data));session.response_finished(before_responses,before_errors,health)
        self.assertEqual(session.counts['fully_valid_candidates'],1)
        self.assertEqual(session.counts['responses_with_graphql_errors'],1)
        self.assertEqual(session.counts['responses_rejected_by_production'],1)
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')

    def test_validation_exception_is_safe_and_does_not_hide_later_candidates(self):
        from app.instagram_scraper import partial_capture
        session=self.session();data=payload(media());second=media();second['code']='OTHER_CODE'
        data['data'][ROOT]['edges'].append({'node':second})
        with patch.object(partial_capture,'_images',side_effect=[RuntimeError('PRIVATE_EXCEPTION'),True]):
            session.observe(data,'graphql_error')
        self.assertEqual(session.counts['diagnostic_errors'],1)
        self.assertEqual(session.reasons['validation_exception'],1)
        self.assertEqual(session.counts['incomplete_candidates'],1)
        self.assertEqual(session.counts['fully_valid_candidates'],1)
        self.assertNotIn('PRIVATE_EXCEPTION',json.dumps(session.summary()))

    def test_missing_media_type_is_not_assumed_non_carousel(self):
        node=media();del node['media_type']
        self.assertEqual(self.observe(node).reasons['media_type_unverified'],1)

    def test_identity_limit_preserves_incomplete_accounting(self):
        session=self.session();second=media();second['code']='SECOND_CODE'
        with patch('app.instagram_scraper.partial_capture.MAX_IDENTITIES',1):
            session.observe(payload(media()),'valid_timeline')
            session.observe(payload(second),'valid_timeline')
        self.assertEqual(session.counts['candidate_media_nodes'],2)
        self.assertEqual(session.counts['fully_valid_candidates'],1)
        self.assertEqual(session.counts['incomplete_candidates'],1)
        self.assertEqual(session.reasons['identity_tracking_limit'],1)
        self.assertEqual(session.counts['diagnostic_errors'],1)

    def test_inspection_is_bounded(self):
        session=self.session();data=payload(media());data['data'][ROOT]['edges']*=140
        session.observe(data,'graphql_error')
        self.assertEqual(session.counts['candidate_media_nodes'],128)
        self.assertEqual(session.counts['inspection_truncated_responses'],1)


class PartialRunnerTests(unittest.TestCase):
    def test_success_failure_empty_and_interruption_are_all_partial_unverified(self):
        original=capture_tests.capture
        for outcome in ('complete','error','empty','interrupted'):
            data=payload(media())
            if outcome=='error':data['errors']=[{'message':'PRIVATE_ERROR'}]
            if outcome=='empty':data={'data':{ROOT:{'edges':[]}}}
            def partial(*args,**kwargs):return original(*args,**kwargs,partial_capture=True)
            # Reuse the database-forbidden browser harness; every external
            # browser and cookie operation is mocked, no discovery execution.
            harness=capture_tests.CaptureOnlyTests()
            with patch.object(capture_tests,'capture',side_effect=partial):
                result,_,_,_=harness.invoke(response(data),interrupted=outcome=='interrupted')
            self.assertEqual(result['status'],'partial_unverified')
            self.assertEqual(result['database_writes'],0)
            self.assertEqual(result['gemini_http_requests'],0);self.assertEqual(result['openai_generation_requests'],0)
            self.assertEqual(result['downstream'],'not_invoked')
            self.assertEqual(result['capture_outcome'],{'complete':'completed','empty':'completed',
                'error':'failed','interrupted':'interrupted'}[outcome])
            self.assertNotIn('PRIVATE',json.dumps(result))

    def test_cli_requires_capture_and_forbids_field_review_or_fixture(self):
        with contextlib.redirect_stderr(io.StringIO()),patch('app.instagram_scraper.schema_capture.capture') as capture:
            for options in ([],['--capture','--review','--fixture','/tmp/unused.json']):
                with self.assertRaises(SystemExit):cli.main(['--profile','dvcatucsd','--partial-capture',*options])
        capture.assert_not_called()

    def test_cli_no_fallback_no_retry_and_no_query_probe(self):
        result={'status':'partial_unverified','capture_outcome':'failed'}
        with patch('app.instagram_scraper.schema_capture.capture',return_value=result) as capture, \
             patch('run_instagram_discovery_pipeline.main') as discovery, \
             patch('app.instagram_scraper.schema_probe.enabled',side_effect=AssertionError('must not inspect queries')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--profile','dvcatucsd','--headless','--capture','--partial-capture']),1)
        capture.assert_called_once_with('dvcatucsd',headless=True,partial_capture=True)
        discovery.assert_not_called()
