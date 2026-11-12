"""Offline all-account collection/review; browser, DB, subprocesses always mocked."""
import contextlib
from datetime import datetime,timedelta,timezone
import io,json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

from app.instagram_scraper.collection_staging import StagingCollector,atomic
from app.instagram_scraper.collection_review import configured_accounts,build_queue,inspect_candidate,record_review
from app.instagram_event_master_projection import parent_marker
from app.instagram_scraper.tempdata import TARGET_URLS
import collect_instagram_posts as cli
from test_instagram_daily_images import observed,payload

NOW=datetime(2026,11,5,20,tzinfo=timezone.utc)


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'private';self.accounts=configured_accounts()
    def collector(self,profile,run='one',now=NOW,mode='initial'):
        return StagingCollector(self.root,run,mode,now,profile=profile)
    def test_real_configured_accounts_reused_without_invented_names(self):
        self.assertEqual(self.accounts,list(dict.fromkeys(TARGET_URLS)));self.assertEqual(len(self.accounts),16)
        self.assertIn('dvcatucsd',self.accounts)
    def test_all_profiles_share_one_bounded_collector_and_isolate_failures(self):
        profiles=self.accounts;calls=[]
        def execute(args):
            calls.append(args.profile)
            return dict(status='failed' if len(calls)==1 else 'completed_staging_only',profile=args.profile,
                        reason='mock',recent_candidates_collected=2)
        args=SimpleNamespace(all_profiles=True,profile=profiles[0],mode='initial')
        with patch.object(cli,'STAGING',self.root),patch.object(cli,'supervise',side_effect=execute),contextlib.redirect_stderr(io.StringIO()):
            result=cli.collect_profiles(args)
        self.assertEqual(calls,profiles);self.assertEqual(result['status'],'partial_failure')
        self.assertEqual(result['gemini_requests'],0);self.assertEqual(result['database_writes'],0)
        self.assertEqual(len(result['profiles']),16)
    def test_account_specific_daily_markers_and_legacy_migration(self):
        c=self.collector(self.accounts[0]);c.observe(payload(observed()),'graphql_error')
        c.finish({'status':'failed','scrape_health':{'reason':'response_capture_failed'}})
        d=self.collector(self.accounts[1],'two',mode='daily')
        self.assertIn(self.accounts[0],d.state['collection_days']);self.assertNotIn(self.accounts[1],d.state['collection_days'])
    def test_compound_ids_do_not_block_collection_or_local_event_screening(self):
        c=self.collector('dvcatucsd');c.observe(payload(observed()),'graphql_error')
        q=build_queue(self.root,NOW);self.assertEqual(q['counts']['image_only'],1)
        r=q['records'][0];self.assertEqual(r['screening']['status'],'eligible')
        self.assertIn('unsupported_media_identity_layout',r['reasons'])
        self.assertEqual(r['status'],'review_required');self.assertFalse(r['ready_for_paid'])
    def test_cross_profile_observations_preserve_own_metadata_and_dedupe(self):
        a=self.collector(self.accounts[0]);a.observe(payload(observed()),'graphql_error')
        b=self.collector(self.accounts[1],'two');value=observed();value['caption']['text']='Workshop November 21, 2026 at 6 PM.'
        b.observe(payload(value),'graphql_error')
        self.assertEqual(len(b.state['records']),1);self.assertEqual(b.counts['duplicate_observations'],1)
        rows=build_queue(self.root,NOW)['records'];self.assertEqual(len(rows),2)
        self.assertNotEqual(rows[0]['object'],rows[1]['object'])
        self.assertTrue(all('accepted_collaboration_unverified' in r['reasons'] for r in rows))
    def test_identity_or_author_conflicts_never_merge(self):
        c=self.collector('dvcatucsd');a=observed();b=observed();b['user']['username']='different'
        c.observe(payload(a,b),'graphql_error');q=build_queue(self.root,NOW)
        self.assertEqual(len(q['records']),2)
        self.assertTrue(all('conflicting_identity_or_author' in r['reasons'] for r in q['records']))
    def test_imported_parent_skips_before_paid_calls_even_compound_unknown(self):
        c=self.collector('dvcatucsd');m=observed();m['code']='DfQZTkcqcoR'
        atomic(self.root/'canonical_imports.json',dict(version=1,captured_at='offline',imports=[dict(parent_id=1669,event_id=16,shortcode=m['code'],marker=parent_marker(1669))]))
        c.observe(payload(m),'graphql_error');r=build_queue(self.root,NOW)['records'][0]
        self.assertEqual(r['status'],'skipped_already_imported');self.assertEqual(r['existing_imports'][0]['event_id'],16)
        self.assertFalse(r['ready_for_import']);self.assertFalse(r['verification']['source_membership'])
    def test_already_analyzed_and_legacy_conflicts_are_not_new_gemini_work(self):
        c=self.collector('dvcatucsd')
        atomic(self.root/'canonical_imports.json',dict(version=1,imports=[],processed=[dict(shortcode='synthetic0',analyzed=True)],legacy_shortcodes=['synthetic1']))
        c.observe(payload(observed(0),observed(1)),'graphql_error');rows=build_queue(self.root,NOW)['records']
        self.assertEqual({r['status'] for r in rows},{'skipped_already_analyzed','review_required'})
        self.assertTrue(any('legacy_import_requires_explicit_adoption' in r['reasons'] for r in rows))
    def test_expired_video_and_missing_date_classifications(self):
        c=self.collector('dvcatucsd');expired=observed();expired['caption']['text']='Workshop October 20, 2026 at 6 PM.'
        video=observed(1);video['media_type']=2
        uncertain=observed(2);uncertain['caption']['text']='Upcoming workshop'
        c.observe(payload(expired,video,uncertain),'graphql_error')
        q=build_queue(self.root,NOW)
        self.assertEqual(q['counts']['skipped_expired'],1);self.assertEqual(q['counts']['skipped_media'],1)
        self.assertEqual(q['counts']['review_required'],1)
    def test_rolling_window_and_unknown_dates_not_admitted(self):
        c=self.collector('dvcatucsd');old=observed();old['taken_at']=int((NOW-timedelta(days=41)).timestamp())
        unknown=observed(1);unknown.pop('taken_at');c.observe(payload(old,unknown),'graphql_error')
        self.assertEqual(c.counts['older_observations_excluded'],1)
        q=build_queue(self.root,NOW);self.assertEqual(len(q['records']),1);self.assertFalse(q['records'][0]['ready_for_paid'])
    def test_explicit_review_intent_never_overrides_missing_proof_and_stale_revision(self):
        now=datetime.now(timezone.utc);m=observed();m['taken_at']=int(now.timestamp());m['caption']['text']='Workshop November 20, 2026 at 6 PM.'
        c=self.collector('dvcatucsd',now=now);c.observe(payload(m),'graphql_error')
        r=build_queue(self.root)['records'][0]
        result=record_review(self.root,r['fingerprint'],'dvcatucsd',r['revision'])
        self.assertEqual(result['status'],'review_recorded');self.assertFalse(result['ready_for_paid'])
        self.assertEqual(record_review(self.root,r['fingerprint'],'dvcatucsd','a'*64)['status'],'rejected')
        self.assertEqual(build_queue(self.root)['records'][0]['approval_request_status'],'pending_verified_evidence')
    def test_invalid_canonical_marker_fails_closed(self):
        c=self.collector('dvcatucsd');c.observe(payload(observed()),'graphql_error')
        atomic(self.root/'canonical_imports.json',dict(version=1,imports=[dict(parent_id=1,event_id=16,shortcode='synthetic0',marker='wrong')]))
        with self.assertRaises(ValueError):build_queue(self.root,NOW)
    def test_cli_preflight_all_accounts_no_lock_browser_or_state_writes(self):
        with patch.object(cli,'collect_profiles') as run,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['--all-profiles','--preflight']),0)
        run.assert_not_called();self.assertEqual(json.loads(output.getvalue())['profiles'],self.accounts)

class ReadOnlyIndexTests(unittest.TestCase):
    def test_snapshot_only_selects_and_read_only_transactions_no_importer(self):
        import sys,types
        from unittest.mock import MagicMock
        import snapshot_instagram_review_index as index
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=dict(DB_HOSTNAME='localhost',DB_PORT='1',DB_USERNAME='offline',DB_PASSWORD='PRIVATE',DB_DATABASE='offline')
            pg=MagicMock();my=MagicMock();pgcur=pg.cursor.return_value.__enter__.return_value;mycur=my.cursor.return_value.__enter__.return_value
            pgcur.fetchall.return_value=[(1669,'dvcatucsd','DfQZTkcqcoR','https://www.instagram.com/p/DfQZTkcqcoR/',True,True,True)]
            mycur.fetchall.return_value=[(16,'Post shortcode: DfQZTkcqcoR<br />Post URL: https://www.instagram.com/p/DfQZTkcqcoR/<br />'+parent_marker(1669))]
            mysql=types.ModuleType('pymysql');mysql.connect=Mock(return_value=my)
            with patch.dict(sys.modules,{'pymysql':mysql}),patch.object(index,'ROOT',root), \
                 patch('dotenv.dotenv_values',return_value=cfg),patch('psycopg2.connect',return_value=pg) as connect:
                result=index.snapshot()
            self.assertEqual(result['canonical_imports'],1);self.assertEqual(result['database_writes'],0)
            self.assertIn('default_transaction_read_only=on',connect.call_args.kwargs['options'])
            pg.set_session.assert_called_once_with(readonly=True,isolation_level='REPEATABLE READ')
            for cur in (pgcur,mycur):
                for call in cur.execute.call_args_list:
                    self.assertTrue(call.args[0].startswith(('SELECT','START TRANSACTION READ ONLY')))
            pg.commit.assert_not_called();my.commit.assert_not_called();pg.rollback.assert_called_once();my.rollback.assert_called_once()
            saved=(root/'.instagram-post-staging/canonical_imports.json').read_text()
            self.assertNotIn('PRIVATE',saved);self.assertNotIn('DB_PASSWORD',saved)
    def test_snapshot_default_preflight_does_not_connect(self):
        import snapshot_instagram_review_index as index
        with patch.object(index,'snapshot') as db,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(index.main([]),0)
        db.assert_not_called()

class BatchExceptionTests(unittest.TestCase):
    def test_worker_start_exception_is_isolated_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def start(args):
                calls.append(args.profile)
                if len(calls)==1:raise RuntimeError('PRIVATE_FAILURE')
                return dict(status='completed_staging_only',profile=args.profile)
            args=SimpleNamespace(all_profiles=True,profile='dvcatucsd',mode='initial')
            with patch.object(cli,'STAGING',Path(tmp)/'staging'),patch.object(cli,'supervise',side_effect=start),contextlib.redirect_stderr(io.StringIO()):
                result=cli.collect_profiles(args)
            self.assertEqual(calls,configured_accounts());self.assertEqual(result['status'],'partial_failure')
            self.assertEqual(result['profiles'][0]['profile'],configured_accounts()[0])
            self.assertNotIn('PRIVATE_FAILURE',json.dumps(result))
