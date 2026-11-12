"""Offline private staging tests; never navigate or connect to a database."""
import contextlib
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,Mock,patch
from zoneinfo import ZoneInfo

from app.instagram_scraper.collection_staging import StagingCollector,window,publication,media_class,CollectionStop
from app.instagram_scraper import schema_capture
import collect_instagram_posts as cli
from test_instagram_daily_pilot import image,data
from test_instagram_scrape_health import response
from app.instagram_scraper import sync_ins as scraper

NOW=datetime(2026,11,5,20,tzinfo=timezone.utc)


def media(code='one',days=1):
    result=image();result.update(code=code,taken_at=int((NOW-timedelta(days=days)).timestamp()))
    return result


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'staging'
    def collector(self,run='a',now=NOW,mode='initial'):
        return StagingCollector(self.root,run,mode,now)
    def test_rolling_cutoff_boundary_and_older_exclusion(self):
        c=self.collector();c.observe(data(media('boundary',40),media('old',41),media('recent',1)),'graphql_error')
        self.assertEqual(len(c.recent),2);self.assertEqual(c.counts['older_observations_excluded'],1)
        self.assertEqual(len(list(c.objects.glob('*.json'))),2)
        self.assertTrue(all(not r['ai_eligible'] for r in c.state['records'].values()))
    def test_la_run_date_dst_and_aware_clock(self):
        for now in (datetime(2026,11,1,8,30,tzinfo=timezone.utc),datetime(2026,11,1,9,30,tzinfo=timezone.utc),
                    datetime(2026,3,8,10,30,tzinfo=timezone.utc)):
            local,cutoff=window(now)
            self.assertEqual(local.date(),now.astimezone(ZoneInfo('America/Los_Angeles')).date())
            self.assertEqual(now-cutoff,timedelta(days=40))
        self.assertEqual(window(datetime(2027,1,1,2,tzinfo=timezone.utc))[0].year,2026)
        with self.assertRaises(ValueError):window(datetime(2026,11,5))
    def test_unknown_conflicting_future_timestamps_quarantined(self):
        missing=media('missing');missing.pop('taken_at')
        conflicting=media('conflict');conflicting['taken_at_timestamp']=1
        future=media('future',-2)
        c=self.collector();c.observe(data(missing,conflicting,future),'unsupported_timeline_schema')
        self.assertEqual(len(c.unknown),3);self.assertEqual(len(c.recent),0)
        self.assertEqual(len((c.runs/'a.quarantine.jsonl').read_text().splitlines()),3)
    def test_numeric_alias_and_cdn_changes_dedupe_preserving_versions(self):
        c=self.collector();one=media();c.observe(data(one),'valid_timeline')
        two=media();two['id']=123;two['pk']='00123'
        two['image_versions2']['candidates'][0]['url']+='?expires=OTHER'
        c.observe(data(two),'valid_timeline')
        self.assertEqual(len(c.recent),1);self.assertEqual(c.counts['duplicate_observations'],1)
        self.assertEqual(len(list(c.objects.glob('*.json'))),2)
        self.assertEqual(c.summary('running','x')['identity_conflicts'],0)
    def test_repeated_daily_runs_retain_identity_and_rolling_window(self):
        c=self.collector();c.observe(data(media('one')),'valid_timeline')
        c.finish({'status':'completed','scrape_health':{'reason':'timeline_verified'}})
        d=self.collector('b',NOW+timedelta(days=1),'daily');d.observe(data(media('one'),media('new')),'valid_timeline')
        self.assertEqual(d.counts['duplicate_observations'],1)
        self.assertEqual(d.counts['new_candidate_fingerprints'],1)
        e=self.collector('c',NOW+timedelta(days=45),'daily')
        self.assertEqual(json.loads((e.root/'active_recent.json').read_text())['candidates'],{})
    def test_conflicting_authors_or_ids_are_never_merged(self):
        c=self.collector();one=media();two=media();two['user']={'username':'another'}
        three=media();three.update(pk=999,id='999')
        c.observe(data(one,two,three),'graphql_error')
        self.assertEqual(len(c.state['records']),3)
        self.assertTrue(all(r['conflict'] for r in c.state['records'].values()))
        self.assertEqual(json.loads((c.root/'active_recent.json').read_text())['candidates'],{})
    def test_compound_ids_stay_blocked_and_no_canonical_identity_invented(self):
        m=media();m['id']='123_456'
        c=self.collector();c.observe(data(m,dict(m)),'graphql_error')
        self.assertEqual(len(c.recent),1)
        record=next(iter(c.state['records'].values()))
        self.assertFalse(record['identity_verified']);self.assertIsNone(record['canonical_media_id'])
        self.assertIn('unsupported_media_identity_layout',record['verification_blockers'])
    def test_media_classification_complete_image_carousel_and_exclusions(self):
        one=media();self.assertEqual(media_class(one),'single_image_fields')
        carousel=media();carousel.update(media_type=8,carousel_media_count=2,carousel_media=[media('a'),media('b')])
        self.assertEqual(media_class(carousel),'image_only_carousel_fields')
        carousel['carousel_media'][1]['media_type']=2
        self.assertEqual(media_class(carousel),'mixed_media_carousel')
        carousel['carousel_media']=[];self.assertEqual(media_class(carousel),'unknown_media')
        one['media_type']=2;self.assertEqual(media_class(one),'video_or_reel')
        one.pop('media_type');self.assertEqual(media_class(one),'unknown_media')
    def test_metadata_private_provenance_saved_but_secrets_and_raw_envelope_not(self):
        m=media();m['cookies']='SECRET';m['authorization']='SECRET'
        c=self.collector();c.observe(data(m,errors=[{'message':'PRIVATE_RESPONSE'}]),'graphql_error')
        saved=''.join(p.read_text() for p in c.root.rglob('*') if p.is_file())
        self.assertNotIn('SECRET',saved);self.assertNotIn('PRIVATE_RESPONSE',saved)
        self.assertIn('Workshop',saved)
        for path in c.root.rglob('*'):self.assertEqual(path.stat().st_mode & 0o077,0)
        self.assertEqual(c.summary('completed','x')['ai_eligible'],0)
    def test_null_unknown_and_malformed_items_not_promoted(self):
        c=self.collector();c.observe(data({'media':None},{'media':[]},{'other':{}},media()),'unsupported_timeline_schema')
        self.assertEqual(c.counts['null_or_malformed_slots'],2)
        self.assertEqual(c.counts['unsupported_slots'],1);self.assertEqual(len(c.recent),1)
    def test_unverified_order_never_stops_and_pins_do_not_stop(self):
        c=self.collector()
        posts=[dict(shortcode=str(i),taken_at=1,is_pinned=False) for i in range(3)]
        for _ in range(4):self.assertFalse(c.chronological_batch(posts))
        self.assertFalse(c.chronological_batch(posts,source_order_verified=True))
        pinned=[dict(shortcode=str(i),taken_at=1,is_pinned=True) for i in range(3,6)]
        self.assertFalse(c.chronological_batch(pinned,source_order_verified=True))
        for start,expected in ((6,False),(9,True)):
            batch=[dict(shortcode=str(i),taken_at=1,is_pinned=False) for i in range(start,start+3)]
            self.assertEqual(c.chronological_batch(batch,source_order_verified=True),expected)
    def test_failed_auth_transport_or_interrupted_never_marks_daily_complete(self):
        for reason in ('login_required','capture_interrupted','capture_execution_failed'):
            c=self.collector(reason);c.observe(data(media()),'graphql_error')
            result=c.finish({'status':'failed','scrape_health':{'reason':reason}})
            self.assertEqual(result['status'],'failed');self.assertIsNone(c.state['last_collection_day'])
        c=self.collector('network');c.observe(data(media()),'graphql_error')
        self.assertEqual(c.finish({'status':'failed','scrape_health':{'reason':'response_capture_failed',
            'error_categories':{'request_failed':1}}})['status'],'failed')
    def test_daily_duplicate_day_skips_before_browser(self):
        c=self.collector();c.state['last_collection_day']=datetime.now(timezone.utc).astimezone(c.local.tzinfo).date().isoformat()
        c.checkpoint('completed_staging_only','x')
        args=Mock(mode='daily',max_scrolls=20,max_seconds=300)
        with patch.object(cli,'STAGING',self.root),patch.object(schema_capture,'capture') as capture:
            result=cli.worker(args,'next')
        capture.assert_not_called();self.assertEqual(result['reason'],'already_collected_today')
    def test_preflight_no_browser_or_storage_and_schedule_disabled(self):
        for args in ([],['--run','--scheduled']):
            with patch.object(cli,'worker') as worker,patch.object(cli,'supervise') as supervise,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args),0)
                worker.assert_not_called();supervise.assert_not_called()


class CaptureIntegrationTests(unittest.TestCase):
    def test_collection_uses_only_passive_browser_helpers_at_most_twenty_scrolls(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=StagingCollector(Path(tmp)/'stage','run','initial',NOW)
            page=MagicMock();page.url='https://www.instagram.com/dvcatucsd/'
            page.locator.return_value.count.return_value=0
            handlers={};page.on.side_effect=lambda event,callback:handlers.update({event:callback})
            page.goto.side_effect=lambda *a,**k:(handlers['response'](response(data(media()))),Mock(status=200))[1]
            runtime=MagicMock();runtime.firefox.launch.return_value.new_context.return_value.new_page.return_value=page
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(scraper,'sync_playwright',return_value=Mock(
                    __enter__=Mock(return_value=runtime),__exit__=Mock(return_value=False))))
                stack.enter_context(patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]))
                scroll=stack.enter_context(patch.object(scraper,'_scroll_profile_down_once'))
                forbidden=[stack.enter_context(patch.object(scraper,name,side_effect=AssertionError('forbidden')))
                    for name in ('upsert_posts_for_profile','known_shortcodes_for_profile','main','_scroll_profile_capture_and_upsert')]
                result=schema_capture.capture('dvcatucsd',headless=True,collection=c,max_scrolls=20,max_seconds=290)
                self.assertEqual(scroll.call_count,20)
                for operation in forbidden:operation.assert_not_called()
                self.assertEqual(result['database_writes'],0);self.assertEqual(len(c.recent),1)
                page.goto.assert_called_once();runtime.firefox.launch.assert_called_once_with(headless=True)
                page.screenshot.assert_not_called()
    def test_supervisor_hard_deadline_terminates_single_worker_no_retry(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp,patch.object(cli,'STAGING',Path(tmp)), \
             patch.object(cli.subprocess,'Popen') as popen,patch.object(cli.os,'killpg') as kill:
            proc=popen.return_value;proc.pid=123
            proc.communicate.side_effect=[subprocess.TimeoutExpired('worker',295),('',None)]
            result=cli.supervise(Mock(mode='initial',max_scrolls=20,max_seconds=300))
            self.assertEqual(result['status'],'interrupted');popen.assert_called_once()
            self.assertFalse(popen.call_args.kwargs['shell']);self.assertTrue(popen.call_args.kwargs['start_new_session'])
            kill.assert_called_once()

    def test_failed_and_interrupted_collection_never_upserts_or_invokes_downstream(self):
        for interruption in (False,True):
            with self.subTest(interruption=interruption),tempfile.TemporaryDirectory() as tmp:
                c=StagingCollector(Path(tmp)/'stage','run','initial',NOW)
                page=MagicMock();page.url='https://www.instagram.com/dvcatucsd/'
                page.locator.return_value.count.return_value=0
                page.goto.side_effect=KeyboardInterrupt() if interruption else RuntimeError('PRIVATE_FAILURE')
                runtime=MagicMock();runtime.firefox.launch.return_value.new_context.return_value.new_page.return_value=page
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(scraper,'sync_playwright',return_value=Mock(
                        __enter__=Mock(return_value=runtime),__exit__=Mock(return_value=False))))
                    stack.enter_context(patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]))
                    forbidden=[stack.enter_context(patch.object(scraper,name,side_effect=AssertionError('forbidden')))
                        for name in ('upsert_posts_for_profile','known_shortcodes_for_profile','main','_scroll_profile_capture_and_upsert')]
                    result=c.finish(schema_capture.capture('dvcatucsd',headless=True,collection=c,max_scrolls=20,max_seconds=290))
                    for operation in forbidden:operation.assert_not_called()
                self.assertEqual(result['status'],'failed');self.assertEqual(result['database_writes'],0)
                self.assertIsNone(c.state['last_collection_day'])
                self.assertNotIn('PRIVATE_FAILURE',json.dumps(result))

    def test_fresh_interpreter_entry_point_cannot_import_database_or_paid_modules(self):
        import subprocess,sys
        program=r'''
import sys,importlib.abc
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        forbidden=('Blueprint_db','scrape_db','sqlalchemy','psycopg2','pymysql','boto3',
            'google','openai','app.ai_analyze_service','app.event_cover_service',
            'app.event_master_sync','app.parent_pipeline.service','app.parent_pipeline.discovery')
        if any(fullname==name or fullname.startswith(name+'.') for name in forbidden):
            raise AssertionError('Forbidden module')
sys.meta_path.insert(0,Guard())
import collect_instagram_posts as cli
from app.instagram_scraper.schema_capture import capture
from app.instagram_scraper import sync_ins
cli.main(['--preflight'])
'''
        run=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout)['gemini_requests'],0)
