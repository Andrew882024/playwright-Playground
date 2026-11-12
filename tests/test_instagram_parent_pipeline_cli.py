"""Offline bounded stored-parent CLI tests; no real DB/network operations."""
import contextlib
import io
import os
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace as Row
from unittest.mock import Mock,MagicMock,patch
from contextlib import ExitStack
from test_instagram_event_extraction import serial
from test_instagram_post_presentation import parent,children
from app.parent_pipeline import service
import run_instagram_parent_pipeline as cli
from app.pipeline_recovery.hooks import current_run
from app.event_cover_service.openai_cover import CoverError


class ParentPipelineTests(unittest.TestCase):
    def fixture(self,analyzed=True):
        p=parent();p.ai_analyzed=analyzed
        return p,children()
    @contextlib.contextmanager
    def mocked(self,analyzed=True):
        with ExitStack() as stack:
            journal=stack.enter_context(tempfile.TemporaryDirectory())
            # All paid-dispatch tests use disposable ledgers, never operator state.
            stack.enter_context(patch.dict(os.environ,{
                'INSTAGRAM_PAID_REQUEST_LEDGER_DIR':str(Path(journal)/'daily'),
                'INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS':'100',
                'INSTAGRAM_DAILY_COVER_MAX_REQUESTS':'100'}))
            stack.enter_context(patch('app.parent_pipeline.attempt_safety.DIRECTORY',Path(journal)))
            stack.enter_context(patch.object(service,'parent_lock',return_value=contextlib.nullcontext(True)))
            p,rows=self.fixture(analyzed)
            read=stack.enter_context(patch.object(service,'read_parent',return_value=(p,rows)))
            analyze=stack.enter_context(patch.object(serial,'main'))
            backfill=stack.enter_context(patch('app.add_our_own_s3_url.add_our_own_s3_url.run_backfill'))
            cover=stack.enter_context(patch('app.event_cover_service.parent_cover.run_parent_cover',return_value=dict(mode='generated')))
            sync=stack.enter_context(patch('app.event_master_sync.sync_service.sync_posts',return_value=dict(
                attempted=1,parents=[dict(status='succeeded',action='unchanged',event_id=15)])))
            stack.enter_context(patch('app.event_cover_service.openai_cover.Settings.from_env',return_value=Mock()))
            yield Row(parent=p,rows=rows,read=read,analyze=analyze,backfill=backfill,cover=cover,sync=sync)

    def test_default_preflight_never_calls_run(self):
        with patch('dotenv.load_dotenv'),patch.object(service,'preflight',return_value=dict(status='preflight')) as preflight, \
             patch.object(service,'run') as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--parent-id','6']),0)
        preflight.assert_called_once_with(6);run.assert_not_called()

    def test_run_requires_flag_and_invalid_ids_rejected(self):
        with patch('dotenv.load_dotenv'),patch.object(service,'run',return_value=dict(status='completed')) as run, \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--parent-id','6','--run']),0)
            for args in (['--parent-id','0'],['--parent-id','6','--preflight','--run']):
                with self.assertRaises(SystemExit):cli.main(args)
        run.assert_called_once_with(6)

    def test_preflight_select_only_no_metadata_or_services(self):
        import Blueprint_db
        p,rows=self.fixture()
        session=MagicMock();session.execute.return_value.scalar_one_or_none.return_value=p
        session.scalars.return_value.all.return_value=rows
        factory=MagicMock();factory.return_value.__enter__.return_value=session
        with patch.object(Blueprint_db,'SessionLocal',factory),patch.object(Blueprint_db.Base.metadata,'create_all') as create:
            result=service.preflight(6)
        self.assertEqual(result['status'],'preflight');create.assert_not_called()
        statements=[str(c.args[0]) for c in session.execute.call_args_list]
        self.assertTrue(statements[0].startswith('SET TRANSACTION'))
        self.assertTrue(statements[1].startswith('SELECT'))
        self.assertIn('instagram_posts.id =',statements[1])
        self.assertNotIn('https://',str(result));session.commit.assert_not_called()

    def test_all_stages_scoped_and_caps_one(self):
        with self.mocked(False) as m:
            def analyze(args,**kwargs):
                self.assertEqual(args,['--post-id','6','--profile','acm.at.ucsd','--limit','1','--sleep','0'])
                self.assertEqual(kwargs['max_attempts'],1)
                kwargs['committed_parent_ids'].add(6);kwargs['analysis_stats'].update(attempted=1,committed=1,failed=0)
                m.parent.ai_analyzed=True
            m.analyze.side_effect=analyze
            result=service.run(6)
        m.analyze.assert_called_once();m.cover.assert_called_once();m.sync.assert_called_once_with([6])
        self.assertEqual(m.backfill.call_args.kwargs['post_id'],6);self.assertEqual(m.backfill.call_args.kwargs['limit'],1)
        self.assertIs(m.cover.call_args.kwargs['generation_guard'],service.require_generation_date)
        self.assertEqual(result['stages']['gemini']['attempted'],1)
        self.assertEqual(result['stages']['scrape']['reason'],'stored_parent_no_scrape')
        for s in result['stages'].values():
            self.assertTrue({'attempted','succeeded','skipped','failed','review_required'}<=s.keys())

    def test_existing_parent_no_gemini_and_import_identity_preserved(self):
        with self.mocked() as m:
            m.cover.return_value=dict(mode='already-published')
            for _ in range(2):
                result=service.run(6)
                self.assertEqual(result['stages']['event_master']['event_id'],15)
        m.analyze.assert_not_called();self.assertEqual(m.sync.call_args.args,([6],))

    def test_expired_missing_dates_and_ambiguity_still_allow_import(self):
        for code in ('skipped_expired','activity_date_review_required','review_required'):
            with self.mocked() as m:
                m.cover.side_effect=CoverError('Safe internal message',reason=code)
                result=service.run(6)
                m.sync.assert_called_once_with([6])
                self.assertEqual(result['stages']['cover']['skipped' if code=='skipped_expired' else 'review_required'],1)

    def test_extraction_failure_stops_downstream(self):
        with self.mocked(False) as m:
            def fail(args,**kwargs):kwargs['analysis_stats'].update(attempted=1,committed=0,failed=1)
            m.analyze.side_effect=fail
            result=service.run(6)
        self.assertEqual(result['status'],'failed')
        m.backfill.assert_not_called();m.cover.assert_not_called();m.sync.assert_not_called()

    def test_zero_activities_no_cover_or_import(self):
        with self.mocked() as m:
            m.read.return_value=(m.parent,[]);result=service.run(6)
        self.assertEqual(result['stages']['event_master']['reason'],'no_activities')
        m.cover.assert_not_called();m.sync.assert_not_called()

    def test_legacy_conflict_not_adopted_and_no_recovery_context(self):
        with self.mocked() as m:
            m.sync.side_effect=lambda ids: dict(attempted=1,parents=[dict(status='adoption-required')]) if current_run.get() is None else self.fail('Recovery must be detached')
            token=current_run.set(Mock())
            try:result=service.run(6)
            finally:current_run.reset(token)
        self.assertEqual(result['stages']['event_master']['review_required'],1)
        self.assertEqual(m.sync.call_args.kwargs,{})

    def test_uploader_query_filters_exact_parent(self):
        import Blueprint_db
        from app.add_our_own_s3_url import add_our_own_s3_url as upload
        factory=MagicMock();db=factory.return_value.__enter__.return_value
        db.scalars.return_value.all.return_value=[]
        with patch.object(upload,'SessionLocal',factory):upload.rows_to_backfill('acm',1,post_id=6)
        compiled=db.scalars.call_args.args[0].compile()
        self.assertIn('instagram_posts.id =',str(compiled));self.assertIn(6,compiled.params.values())
