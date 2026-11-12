import unittest
from unittest.mock import patch, Mock
from test_instagram_response_policy import profile, reply, PROFILE_ROOT
from app.instagram_scraper import sync_ins as scraper
from app.instagram_scraper import scrape_db

class BestEffortTests(unittest.TestCase):
    def test_bad_response_does_not_block_valid_sibling_but_stays_partial(self):
        h=scraper.new_capture_health();h['best_effort']=True;b=[];c=set()
        handler,_=scraper._make_graphql_posts_capture(b,c,h)
        d=profile();d['errors'][0]['path'][-1]='unknown';handler(reply(d))
        self.assertIsNotNone(scraper.capture_failure(h))
        d=profile();del d['errors'];handler(reply(d))
        self.assertIsNone(scraper.capture_failure(h));self.assertEqual(h['capture_errors'],1)
        self.assertEqual(len(b),1)

    def test_authentication_failure_still_blocks(self):
        h=scraper.new_capture_health();h.update(best_effort=True,valid_timeline_responses=1)
        h['http_status_codes']=[403]
        self.assertEqual(scraper.capture_failure(h),'global_capture_failure')
        h['http_status_codes']=[];h['graphql_error_diagnostics']['code_counts']['190']=1
        self.assertEqual(scraper.capture_failure(h),'global_capture_failure')

    def test_bad_post_skipped_valid_sibling_retained(self):
        h=scraper.new_capture_health();h['best_effort']=True;b=[];c=set()
        handler,_=scraper._make_graphql_posts_capture(b,c,h)
        d=profile();del d['errors'];d['data'][PROFILE_ROOT]['edges'].append({'node':{'code':'bad','taken_at':None}})
        handler(reply(d));self.assertIsNone(scraper.capture_failure(h))
        self.assertEqual(c,{'sample_post'});self.assertEqual(h['post_warnings'][0]['post_id'],'bad')

    def test_per_post_failure_does_not_prevent_next_transaction(self):
        good={'shortcode':'a','taken_at':1794438000,'openable_media_urls':{'largest':['https://example.test/a.jpg']}}
        other=dict(good,shortcode='b')
        original=scrape_db.upsert_posts_for_profile; report={};ids=[]
        def transaction(profile,posts,**kw):
            if posts[0]['shortcode']=='a':raise ValueError('private database details')
            kw['report'].update(inserted=1,updated=0,unchanged=0);kw['inserted_parent_ids'].append(123)
        with patch.object(scrape_db,'upsert_posts_for_profile',side_effect=transaction):
            original('sample',[good,other],best_effort=True,report=report,inserted_parent_ids=ids)
        self.assertEqual(ids,[123]);self.assertEqual(report['failed'],1);self.assertEqual(report['inserted'],1)
        self.assertNotIn('private',str(report))

    def test_database_unavailable_is_global(self):
        from sqlalchemy.exc import OperationalError
        original=scrape_db.upsert_posts_for_profile
        post={'shortcode':'a','taken_at':1794438000,'openable_media_urls':{'largest':['https://example.test/a.jpg']}}
        with patch.object(scrape_db,'upsert_posts_for_profile',side_effect=OperationalError('sql',{},Exception())):
            with self.assertRaises(OperationalError):original('sample',[post],best_effort=True)

class IntegrationTests(unittest.TestCase):
    def test_partial_capture_advances_to_screening_without_claiming_complete(self):
        from contextlib import nullcontext
        from app.parent_pipeline import integration
        captured={'status':'partial','inserted':1,'updated':0,'unchanged':0,
                  'profiles':[{'inserted_parent_ids':[123]}],
                  'scrape_health':{'status':'partial'}}
        with patch.object(integration,'configured_profiles',return_value=['sixthcoboard']), \
             patch.object(integration,'advisory_lock',return_value=nullcontext(True)), \
             patch.object(integration,'candidates',return_value=[]) as select_candidates, \
             patch.object(scraper,'main',return_value=captured) as scrape:
            result=integration.execute.__wrapped__(profile='sixthcoboard',run=True,best_effort=True,headless=True)
        select_candidates.assert_called_once()
        self.assertIn('--best-effort',scrape.call_args.args[0])
        self.assertEqual(result['status'],'partial_failure')
        self.assertEqual(result['inserted'],1)
