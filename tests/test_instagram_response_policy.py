"""Sanitized observed response shapes; all I/O mocked."""
import copy
import json
import unittest
from unittest.mock import Mock
from test_instagram_event_extraction import serial
from app.instagram_scraper import sync_ins as scraper
from app.instagram_scraper.response_policy import PROFILE_ROOT, HOME_ROOT, FIELD_EXCEPTION

PROFILE_OP='PolarisProfilePostsTabContentQuery_connection'
HOME_OP='PolarisFeedTimelineRootV2Query'


def profile():
    return {'data': {PROFILE_ROOT: {'edges': [{'node': {
        'code': 'sample_post', 'id': '123', 'taken_at': 1794438000,
        'caption': {'text': 'Workshop November 20 at 6pm'},
        'user': {'username': 'sample_account'},
        'image_versions2': {'candidates': [{'url': 'https://example.cdninstagram.com/post.jpg'}]},
        'location': {'name': 'Campus', 'profile_pic_url': None}}}]}},
        'errors': [{'message': FIELD_EXCEPTION, 'severity': 'ERROR',
                    'path': [PROFILE_ROOT, 'edges', 0, 'node', 'location', 'profile_pic_url']}]}


def reply(data,op=PROFILE_OP):
    return Mock(url='https://www.instagram.com/api/graphql',status=200,
        text=Mock(return_value=json.dumps(data)),request=Mock(method='POST',
        post_data='fb_api_req_friendly_name='+op))


class PolicyTests(unittest.TestCase):
    def capture(self,data,op=PROFILE_OP):
        h=scraper.new_capture_health();b=[];c=set()
        handler,_=scraper._make_graphql_posts_capture(b,c,h);handler(reply(data,op))
        return h,b,c

    def test_exact_leaf_error_retains_posts_and_error_diagnostics(self):
        data=profile();before=copy.deepcopy(data);h,b,c=self.capture(data)
        self.assertIsNone(scraper.capture_failure(h))
        self.assertEqual(h['graphql_error_diagnostics']['responses'],1)
        self.assertEqual(h['location_avatar_error_responses'],1)
        self.assertEqual(c,{'sample_post'});self.assertEqual(data,before)
        posts=scraper.structured_bundle_from_graphql_bodies(b,'sample_account')['posts']
        self.assertEqual(posts[0]['shortcode'],'sample_post')
        self.assertNotIn(FIELD_EXCEPTION,b[0])

    def test_other_errors_and_missing_leaf_remain_blocked(self):
        cases=[]
        for tail in (['caption','text'],['image_versions2','candidates'],['location'],['user','profile_pic_url'],['location','unknown']):
            d=profile();d['errors'][0]['path']=[PROFILE_ROOT,'edges',0,'node',*tail];cases.append((d,PROFILE_OP))
        for index in (-1,True,99):
            d=profile();d['errors'][0]['path'][2]=index;cases.append((d,PROFILE_OP))
        d=profile();d['errors'][0]['message']='Authentication failed';cases.append((d,PROFILE_OP))
        d=profile();d['errors'][0]['code']=190;cases.append((d,PROFILE_OP))
        for location in (None,{}, {'profile_pic_url':'https://example.test/avatar.jpg'}):
            d=profile();d['data'][PROFILE_ROOT]['edges'][0]['node']['location']=location;cases.append((d,PROFILE_OP))
        cases.append((profile(),'OtherQuery'))
        d=profile();d['errors'].append({'path':[PROFILE_ROOT,'page_info']});cases.append((d,PROFILE_OP))
        for d,op in cases:
            with self.subTest(d=d,op=op):
                h,b,c=self.capture(d,op)
                self.assertEqual(h['error_categories']['graphql_error'],1)
                self.assertEqual(b,[]);self.assertEqual(c,set())

    def test_avatar_exception_does_not_mask_broken_media_siblings(self):
        for edge in ({'node':None},{'node':{'unknown':{}}},{'node':{'code':17}}):
            d=profile();d['data'][PROFILE_ROOT]['edges'].append(edge)
            h,b,c=self.capture(d)
            self.assertEqual(scraper.capture_failure(h),'response_capture_failed')
            self.assertEqual(b,[]);self.assertEqual(c,set())

    def test_home_feed_exclusion_requires_exact_root_and_operation(self):
        d={'data':{HOME_ROOT:{'edges':[{'node':{'media':None}},{'node':{'media':{'code':'foreign_post'}}}]}}}
        h,b,c=self.capture(d,HOME_OP)
        self.assertEqual(h['ignored_home_feed_responses'],1)
        self.assertEqual(h['capture_errors'],0);self.assertEqual(b,[]);self.assertEqual(c,set())
        self.assertEqual(scraper.capture_failure(h),'no_valid_timeline_response')
        for op in ('OtherQuery',PROFILE_OP):
            h,b,c=self.capture(d,op);self.assertEqual(h['error_categories']['unsupported_timeline_schema'],1)
        d['errors']=[{'message':'unknown'}]
        h,b,c=self.capture(d,HOME_OP);self.assertEqual(h['error_categories']['graphql_error'],1)

    def test_valid_responses_do_not_mask_unknown_errors(self):
        h=scraper.new_capture_health();b=[];c=set();handler,_=scraper._make_graphql_posts_capture(b,c,h)
        d=profile();del d['errors'];handler(reply(d))
        d=profile();d['errors'][0]['path'][-1]='unknown';handler(reply(d))
        self.assertEqual(h['valid_timeline_responses'],1)
        self.assertEqual(scraper.capture_failure(h),'response_capture_failed')

    def test_ambiguous_metadata_stays_blocked(self):
        h=scraper.new_capture_health();handler,_=scraper._make_graphql_posts_capture([],set(),h)
        r=reply(profile());r.request.post_data+='&fb_api_req_friendly_name=OtherQuery'
        handler(r);self.assertEqual(h['error_categories']['graphql_error'],1)

if __name__=='__main__':unittest.main()
