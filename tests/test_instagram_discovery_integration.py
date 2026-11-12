"""Offline discovery orchestration, shared budgets and concurrency regressions."""
import contextlib
import io
import json
import unittest
import tempfile
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import Mock,MagicMock,patch
from types import SimpleNamespace as Row
from test_instagram_event_extraction import serial
from app.parent_pipeline import integration as flow,service
from app.parent_pipeline.budget import RunBudget
from app.parent_pipeline.locks import advisory_lock
import run_instagram_discovery_pipeline as cli


class DiscoveryTests(unittest.TestCase):
    def test_preflight_never_locks_scrapes_or_processes(self):
        with patch.object(flow,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(flow,'candidates',return_value=[]) as candidates, \
             patch.object(flow,'advisory_lock') as lock,patch.object(flow,'run_parent') as run, \
             patch('app.instagram_scraper.sync_ins.main') as scrape:
            result=flow.execute(profile='dvcatucsd')
        self.assertEqual(result['status'],'preflight')
        lock.assert_not_called();run.assert_not_called();scrape.assert_not_called()

    def test_new_parents_and_pending_share_global_attempt_budgets(self):
        budgets=[]
        def parent(parent_id,**kwargs):
            budget=kwargs['budget'];budgets.append(budget)
            budget.consume('gemini');budget.consume('cover')
            return dict(status='completed',parent_id=parent_id,stages={})
        env={'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS':'1','INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS':'1'}
        with patch.dict(flow.os.environ,env),patch.object(flow,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(flow,'advisory_lock',side_effect=lambda *a:contextlib.nullcontext(True)), \
             patch('app.instagram_scraper.sync_ins.main',return_value=dict(inserted=1,updated=2,unchanged=3,
                 profiles=[dict(inserted_parent_ids=[20])])) as scrape, \
             patch.object(flow,'candidates',return_value=[dict(parent_id=i,eligible=True,reason='pending_parent') for i in (20,6,7)]) as candidates, \
             patch.object(flow,'run_parent',side_effect=parent) as run:
            result=flow.execute(profile='dvcatucsd',run=True)
        scrape.assert_called_once_with(['--profile','dvcatucsd']);run.assert_called_once()
        self.assertEqual(candidates.call_args.args[2],{20})
        self.assertEqual(result['gemini_attempts'],1);self.assertEqual(result['cover_attempts'],1)
        self.assertEqual(result['skipped'],2);self.assertEqual(result['inserted'],1)
        self.assertTrue(run.call_args.kwargs['pending_only'])

    def test_failure_consumes_budget_and_does_not_expand_to_other_parents(self):
        budget=RunBudget(1,1)
        self.assertTrue(budget.consume('gemini'));self.assertFalse(budget.consume('gemini'))
        self.assertEqual(budget.gemini_attempts,1)
        self.assertTrue(budget.consume('cover'));self.assertFalse(budget.consume('cover'))

    def test_busy_run_never_opens_browser_or_processes(self):
        with patch.object(flow,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(flow,'advisory_lock',return_value=contextlib.nullcontext(False)), \
             patch('app.instagram_scraper.sync_ins.main') as scrape,patch.object(flow,'run_parent') as run:
            result=flow.execute(run=True)
        self.assertEqual(result['status'],'busy');scrape.assert_not_called();run.assert_not_called()

    def test_parent_busy_does_not_invoke_any_stage(self):
        with patch.object(service,'parent_lock',return_value=contextlib.nullcontext(False)),patch.object(service,'_run') as run:
            result=service.run(6)
        self.assertEqual(result['reason'],'parent_busy');run.assert_not_called()

    def test_configured_caps_cannot_be_increased_by_cli(self):
        with patch.dict(flow.os.environ,{'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS':'1'}):
            self.assertEqual(flow.cap('INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS',10),1)
            self.assertEqual(flow.cap('INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS',0),0)
        with patch.dict(flow.os.environ,{'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS':''}):
            self.assertEqual(flow.cap('INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS',None),1)

    def test_advisory_lock_releases_on_failure(self):
        import Blueprint_db
        engine=MagicMock();connection=engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.scalar.return_value=True
        with patch.object(Blueprint_db,'engine',engine):
            with self.assertRaises(RuntimeError):
                with advisory_lock(1,6) as held:
                    self.assertTrue(held);raise RuntimeError('offline')
        sql=[str(c.args[0]) for c in connection.execute.call_args_list]
        self.assertIn('pg_try_advisory_lock',sql[0]);self.assertIn('pg_advisory_unlock',sql[1])

    def test_candidate_query_skips_analyzed_and_filters_profiles_and_dates(self):
        import Blueprint_db
        p=Row(id=20,profile_username='dvcatucsd',caption='Workshop on November 20, 2099.',post_title='',additional_image_urls={'largest':['https://cdninstagram.com/a.jpg']})
        factory=MagicMock();db=factory.return_value.__enter__.return_value
        db.scalars.return_value.all.side_effect=[[p],[]]
        with patch.object(Blueprint_db,'SessionLocal',factory):
            result=flow.candidates(['dvcatucsd'],datetime.now(timezone.utc),{20})
        self.assertEqual(result[0]['reason'],'explicit_future_event_date');self.assertEqual(result[0]['discovery'],'new_parent')
        for call in db.scalars.call_args_list:
            sql=str(call.args[0]);self.assertIn('ai_analyzed IS false',sql)
            self.assertIn('profile_username IN',sql);self.assertIn('coalesce',sql)
        db.commit.assert_not_called()

    def test_cli_default_preflight_explicit_run_only(self):
        with patch('dotenv.load_dotenv'),patch.object(flow,'execute',return_value=dict(status='preflight')) as execute, \
             contextlib.redirect_stdout(io.StringIO()):
            cli.main(['--profile','dvcatucsd'])
        self.assertFalse(execute.call_args.kwargs['run'])

    def test_actual_parent_runner_reuses_global_cover_budget_across_two_posts(self):
        from test_instagram_post_presentation import parent,children
        from contextlib import ExitStack
        from app.event_cover_service import parent_cover
        budget=RunBudget(2,1)
        with ExitStack() as stack:
            stack.enter_context(patch.object(service,'parent_lock',side_effect=lambda *a:contextlib.nullcontext(True)))
            stack.enter_context(patch('app.parent_pipeline.attempt_safety.review_required',return_value=False))
            stack.enter_context(patch('app.parent_pipeline.attempt_safety.record'))
            read=stack.enter_context(patch.object(service,'read_parent'))
            analyze=stack.enter_context(patch.object(serial,'main'))
            stack.enter_context(patch('app.add_our_own_s3_url.add_our_own_s3_url.run_backfill'))
            cover=stack.enter_context(patch.object(parent_cover,'run_parent_cover',return_value=dict(mode='generated')))
            stack.enter_context(patch('app.event_cover_service.openai_cover.Settings.from_env'))
            sync=stack.enter_context(patch('app.event_master_sync.sync_service.sync_posts',return_value=dict(
                attempted=1,parents=[dict(status='succeeded',action='unchanged',event_id=16)])))
            outcomes=[]
            for parent_id in (6,7):
                p=parent(id=parent_id);p.ai_analyzed=False;p.own_s3_url_for_main_image='original-s3'
                rows=children()
                for row in rows:row.instagram_post_id=parent_id
                read.return_value=(p,rows)
                def extract(args,*,committed_parent_ids,analysis_stats,**kw):
                    p.ai_analyzed=True;committed_parent_ids.add(parent_id)
                    analysis_stats.update(attempted=1,committed=1,failed=0)
                analyze.side_effect=extract
                outcomes.append(service.run(parent_id,budget=budget,pending_only=True))
            self.assertEqual(analyze.call_count,2);cover.assert_called_once();self.assertEqual(sync.call_count,2)
            self.assertEqual(outcomes[1]['stages']['cover']['reason'],'global_cover_budget_exhausted')
            self.assertEqual(budget.gemini_attempts,2);self.assertEqual(budget.cover_attempts,1)

    def test_racing_already_analyzed_parent_consumes_no_budget(self):
        p=Row(ai_analyzed=True)
        budget=RunBudget(1,1)
        with patch.object(service,'parent_lock',return_value=contextlib.nullcontext(True)), \
             patch.object(service,'read_parent',return_value=(p,[])):
            result=service.run(6,budget=budget,pending_only=True)
        self.assertEqual(result['reason'],'already_analyzed')
        self.assertEqual(budget.gemini_attempts,0);self.assertEqual(budget.cover_attempts,0)

    def test_ambiguous_automatic_analysis_is_reviewed_without_new_paid_call(self):
        p=Row(ai_analyzed=False)
        budget=RunBudget(1,1)
        with patch.object(service,'parent_lock',return_value=contextlib.nullcontext(True)), \
             patch.object(service,'read_parent',return_value=(p,[])), \
             patch('app.parent_pipeline.attempt_safety.review_required',return_value=True), \
             patch.object(serial,'main') as analyze:
            result=service.run(6,budget=budget,pending_only=True)
        self.assertEqual(result['status'],'review_required');analyze.assert_not_called()
        self.assertEqual(budget.gemini_attempts,0)

    def test_durable_intent_is_fail_closed_and_does_not_store_sensitive_payloads(self):
        import tempfile
        from pathlib import Path
        from app.parent_pipeline import attempt_safety
        with tempfile.TemporaryDirectory() as tmp,patch.object(attempt_safety,'DIRECTORY',Path(tmp)):
            self.assertFalse(attempt_safety.review_required(6))
            attempt_safety.record(6,'in_progress');self.assertTrue(attempt_safety.review_required(6))
            attempt_safety.record(6,'review_required');self.assertTrue(attempt_safety.review_required(6))
            attempt_safety.record(6,'retryable');self.assertFalse(attempt_safety.review_required(6))
            (Path(tmp)/'6.json').write_text('invalid')
            self.assertTrue(attempt_safety.review_required(6))


class DiscoveryStatusTests(unittest.TestCase):
    def setUp(self):
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        log_dir=stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.dict(flow.os.environ,{'INSTAGRAM_DISCOVERY_LOG_DIR':str(Path(log_dir)/'runs')}))
        stack.enter_context(patch.dict(flow.os.environ,{
            'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS':'1',
            'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS':'1',
            'INSTAGRAM_PIPELINE_GENERATE_COVERS':'false',
            'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER':'false',
            'INSTAGRAM_PIPELINE_RECOVER_COVERS':'false',
            'INSTAGRAM_PIPELINE_RECOVER_EVENT_MASTER':'false'}))
        stack.enter_context(patch('dotenv.load_dotenv'))
        stack.enter_context(patch.object(flow,'configured_profiles',return_value=['dvcatucsd']))
        self.lock=stack.enter_context(patch.object(flow,'advisory_lock',side_effect=lambda *a:contextlib.nullcontext(True)))
        self.scrape=stack.enter_context(patch('app.instagram_scraper.sync_ins.main',return_value=dict(
            status='completed',inserted=0,updated=0,unchanged=8,profiles=[])))
        selected=[dict(parent_id=i,eligible=False,reason='inferred_event_dates_expired',
                       screening=dict(status='skipped_expired')) for i in (457,458,461,462,463)]
        selected+=[dict(parent_id=459,eligible=False,reason='unanchored_weekday',screening=dict(status='review_required')),
                   dict(parent_id=460,eligible=True,reason='inferred_future_event_date',screening=dict(status='eligible'))]
        self.candidates=stack.enter_context(patch.object(flow,'candidates',return_value=selected))
        self.parent=stack.enter_context(patch.object(flow,'run_parent'))

    def execute(self,**kwargs):
        return flow.execute(profile='dvcatucsd',run=True,gemini_cap=kwargs.get('gemini_cap',0),cover_cap=0)

    def call_cli(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=cli.main(['--profile','dvcatucsd','--run','--gemini-max-attempts','0','--cover-max-attempts','0'])
        return code,json.loads(output.getvalue())

    def test_zero_budget_with_review_and_expired_posts_is_success(self):
        result=self.execute()
        self.assertEqual(result['status'],'completed')
        self.assertEqual({k:result[k] for k in ('inserted','updated','unchanged','processed','failed','review_required','skipped')},
                         dict(inserted=0,updated=0,unchanged=8,processed=0,failed=0,review_required=1,skipped=6))
        for key in ('gemini_attempts','gemini_max_attempts','cover_attempts','cover_max_attempts'):
            self.assertEqual(result[key],0)
        eligible=next(p for p in result['parents'] if p['parent_id']==460)
        self.assertEqual(eligible['reason'],'global_gemini_budget_exhausted')
        self.assertEqual(eligible['screening']['status'],'eligible')
        self.assertEqual(result['recovery'],'disabled_for_this_command')
        self.parent.assert_not_called();self.scrape.assert_called_once_with(['--profile','dvcatucsd'])
        self.lock.assert_called_once_with(flow.RUN_LOCK,1)

    def test_zero_budget_cli_exits_successfully(self):
        code,result=self.call_cli()
        self.assertEqual(code,0);self.assertEqual(result['status'],'completed')
        self.assertEqual(result['review_required'],1)
        self.parent.assert_not_called()

    def test_zero_budget_without_candidates_is_success(self):
        self.candidates.return_value=[]
        result=self.execute()
        self.assertEqual(result['status'],'completed');self.assertEqual(result['failed'],0)
        self.parent.assert_not_called()

    def test_screening_reviews_are_not_execution_failures_with_positive_budget(self):
        self.candidates.return_value=[self.candidates.return_value[-2]]
        result=self.execute(gemini_cap=1)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['review_required'],1)
        self.assertEqual(result['gemini_attempts'],0);self.parent.assert_not_called()

    def test_actual_parent_stage_failure_remains_partial_failure(self):
        def fail(parent_id,**kwargs):
            self.assertTrue(kwargs['budget'].consume('gemini'))
            return dict(parent_id=parent_id,status='partial_failure',stages={
                'cover':dict(attempted=1,failed=1,reason='cover_failed')})
        self.parent.side_effect=fail
        result=self.execute(gemini_cap=1)
        self.assertEqual(result['status'],'partial_failure');self.assertEqual(result['failed'],1)
        self.assertEqual(result['review_required'],1);self.assertEqual(result['gemini_attempts'],1)
        self.assertEqual(result['parents'][-1]['stages']['cover']['reason'],'cover_failed')

    def test_parent_exception_remains_visible_but_safe(self):
        self.parent.side_effect=RuntimeError('PRIVATE_TEST_EXCEPTION_PAYLOAD')
        result=self.execute(gemini_cap=1)
        self.assertEqual(result['status'],'partial_failure');self.assertEqual(result['failed'],1)
        self.assertEqual(result['parents'][-1]['reason'],'parent_execution_failed')
        self.assertNotIn('PRIVATE_TEST_EXCEPTION_PAYLOAD',json.dumps(result))

    def test_partial_failure_from_executed_parent_is_preserved_without_failed_counter(self):
        self.parent.return_value=dict(parent_id=460,status='partial_failure',stages={
            'event_master':dict(failed=0,review_required=1,reason='adoption-required')})
        result=self.execute(gemini_cap=1)
        self.assertEqual(result['status'],'partial_failure');self.assertEqual(result['failed'],0)
        self.assertEqual(result['parents'][-1]['stages']['event_master']['reason'],'adoption-required')

    def test_scraper_and_database_errors_still_fail_cli_with_zero_budget(self):
        from sqlalchemy.exc import OperationalError
        for target in (self.scrape,self.candidates):
            with self.subTest(stage='scrape' if target is self.scrape else 'candidates'):
                target.side_effect=OperationalError('PRIVATE_TEST_QUERY',{},Exception('PRIVATE_TEST_CREDENTIAL'))
                code,result=self.call_cli()
                self.assertEqual(code,1);self.assertEqual(result['status'],'failed')
                self.assertEqual(result['reason'],'discovery_or_configuration_failed')
                self.assertNotIn('PRIVATE_TEST',json.dumps(result))
                self.parent.assert_not_called()
                target.side_effect=None

    def test_legacy_console_output_is_suppressed_without_hiding_json_outcomes(self):
        scraped=self.scrape.return_value
        def scrape(args):
            print('PRIVATE_TEST_SIGNED_URL')
            print('PRIVATE_TEST_COOKIE',file=__import__('sys').stderr)
            return scraped
        self.scrape.side_effect=scrape
        code,result=self.call_cli()
        self.assertEqual(code,0);self.assertEqual(result['unchanged'],8)
        self.assertNotIn('PRIVATE_TEST',json.dumps(result))
