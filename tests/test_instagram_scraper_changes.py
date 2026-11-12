"""Offline scraper CLI and atomic classification tests; no browser/database runs."""
import contextlib
import copy
import io
import json
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as Row
from unittest.mock import MagicMock,Mock,patch
from test_instagram_event_extraction import serial
from app.instagram_scraper import sync_ins as scraper
import scrape_db as db


def post(**changes):
    return dict(shortcode='one',taken_at=1793656800,title='Workshop',media_pk='123',
        comments=[dict(kind='caption',text='Learn Python.')],
        openable_media_urls=dict(largest=['https://one.cdninstagram.com/path/post.jpg?sig=old'],other=[]),**changes)


class ScrapeChangeTests(unittest.TestCase):
    def upsert(self,posts,existing=None):
        session=MagicMock();factory=MagicMock();factory.return_value.__enter__.return_value=session
        if existing is None:session.execute.return_value.scalar_one_or_none.return_value=88
        else:
            session.execute.side_effect=[Row(scalar_one_or_none=lambda:None),Row(scalar_one=lambda:existing),MagicMock()]
        counts={}
        with patch.object(db,'SessionLocal',factory):total=db.upsert_posts_for_profile('dvcatucsd',posts,report=counts)
        return counts,total,session
    def existing(self,p):
        values=db._row_values('dvcatucsd',p)
        return Row(id=1669,ai_analyzed=True,generated_image_url='keep-cover',**values)
    def test_new_post_reports_inserted_and_unique_constraint(self):
        counts,total,s=self.upsert([post()])
        self.assertEqual(counts,dict(inserted=1,updated=0,unchanged=0));self.assertEqual(total,1)
        sql=str(s.execute.call_args.args[0]);self.assertIn('ON CONFLICT ON CONSTRAINT',sql)
        self.assertIn('DO NOTHING',sql);self.assertIn('RETURNING instagram_posts.id',sql)
        s.commit.assert_called_once()
    def test_meaningful_edit_updates_only_scrape_columns_and_preserves_id(self):
        old=self.existing(post());changed=post();changed['comments'][0]['text']='New workshop description.'
        counts,total,s=self.upsert([changed],old)
        self.assertEqual(counts['updated'],1);self.assertEqual(old.id,1669)
        statement=s.execute.call_args_list[-1].args[0];sql=str(statement)
        self.assertTrue(sql.startswith('UPDATE'));self.assertIn(1669,statement.compile().params.values())
        for name in ('main_image_url','own_s3_url_for_main_image','ai_analyzed','generated_image_url','event_title','collection_title'):
            self.assertNotIn(name+'=',sql)
        self.assertEqual(old.generated_image_url,'keep-cover')
        self.assertIn('FOR UPDATE',str(s.execute.call_args_list[1].args[0]))
    def test_unchanged_does_not_issue_update(self):
        counts,total,s=self.upsert([post()],self.existing(post()))
        self.assertEqual(counts['unchanged'],1);self.assertEqual(s.execute.call_count,2)
    def test_cdn_signature_host_variant_and_order_changes_not_meaningful(self):
        old=self.existing(post());changed=post()
        changed['openable_media_urls']=dict(largest=['https://two.cdninstagram.com/other-path/post.jpg?sig=new'],
                                          other=['https://three.fbcdn.net/post.jpg?resize=320'])
        counts,total,s=self.upsert([changed],old)
        self.assertEqual(counts['unchanged'],1);self.assertEqual(counts['updated'],0)
        self.assertIn('additional_image_urls',str(s.execute.call_args_list[-1].args[0]))
    def test_different_media_asset_is_meaningful(self):
        changed=post();changed['openable_media_urls']['largest']=['https://one.cdninstagram.com/new-image.jpg']
        self.assertEqual(self.upsert([changed],self.existing(post()))[0]['updated'],1)
    def test_repeated_shortcode_counted_once_without_duplicate_insert(self):
        counts,total,s=self.upsert([post(),post()])
        self.assertEqual(total,1);self.assertEqual(counts['inserted'],1);self.assertEqual(s.execute.call_count,1)
    def test_inserted_ids_are_published_only_after_commit(self):
        factory=MagicMock();s=factory.return_value.__enter__.return_value
        s.execute.return_value.scalar_one_or_none.return_value=1885
        ids=[]
        with patch.object(db,'SessionLocal',factory):
            db.upsert_posts_for_profile('dvcatucsd',[post()],inserted_parent_ids=ids)
        self.assertEqual(ids,[1885])
        ids=[];s.commit.side_effect=RuntimeError('offline')
        with patch.object(db,'SessionLocal',factory),self.assertRaises(RuntimeError):
            db.upsert_posts_for_profile('dvcatucsd',[post()],inserted_parent_ids=ids)
        self.assertEqual(ids,[])

    def test_failed_commit_does_not_publish_counts(self):
        factory=MagicMock();s=factory.return_value.__enter__.return_value
        s.execute.return_value.scalar_one_or_none.return_value=88;s.commit.side_effect=RuntimeError('offline')
        counts={}
        with patch.object(db,'SessionLocal',factory),self.assertRaises(RuntimeError):
            db.upsert_posts_for_profile('dvcatucsd',[post()],report=counts)
        self.assertEqual(counts,{})


class ScraperProfileTests(unittest.TestCase):
    def run_main(self,args):
        temp=MagicMock()
        browser=temp.return_value.__enter__.return_value
        context=browser.firefox.launch_persistent_context.return_value
        page=MagicMock();context.pages=[page]
        with patch.object(scraper,'TARGET_URLS',['dvcatucsd','acm.at.ucsd']), \
             patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]), \
             patch.object(scraper,'sync_playwright',temp),patch.object(scraper,'SCREENSHOT_DIR',MagicMock()), \
             patch.object(scraper,'PERSISTENT_PROFILE_DIR',MagicMock()), \
             patch.object(scraper,'_scroll_profile_capture_and_upsert',side_effect=lambda p,u,url:dict(profile=u,inserted=1,updated=2,unchanged=3)) as scrape, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result=scraper.main(args)
        return result,scrape,output.getvalue()
    def test_single_profile_isolation_json_and_no_config_mutation(self):
        result,scrape,output=self.run_main(['--profile','dvcatucsd'])
        self.assertEqual(scrape.call_count,1);self.assertEqual(scrape.call_args.args[1],'dvcatucsd')
        self.assertEqual(result['inserted'],1);self.assertEqual(result['updated'],2);self.assertEqual(result['unchanged'],3)
        self.assertEqual(json.loads(output.splitlines()[-1]),result)
    def test_default_all_profiles_and_programmatic_argv_isolation(self):
        result,scrape,_=self.run_main([])
        self.assertEqual([c.args[1] for c in scrape.call_args_list],['dvcatucsd','acm.at.ucsd'])
        self.assertEqual(result['inserted'],2)
        with patch('sys.argv',['uvicorn','app.main:app','--port','8001']):
            self.assertEqual(self.run_main(None)[1].call_count,2)
    def test_unconfigured_profile_rejected_before_browser_or_database(self):
        with patch.object(scraper,'TARGET_URLS',['dvcatucsd']),patch.object(scraper,'sync_playwright') as browser, \
             patch.object(scraper,'load_instagram_cookies_for_playwright') as cookies,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):scraper.main(['--profile','unknown'])
        browser.assert_not_called();cookies.assert_not_called()
