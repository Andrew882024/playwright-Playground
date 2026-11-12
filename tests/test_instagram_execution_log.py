"""Discovery audit tests: temporary state and mocked stages/providers only."""
import contextlib
import io
import json
import multiprocessing
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_instagram_event_extraction import serial
from app.parent_pipeline import execution_log as log, integration
import run_instagram_discovery_pipeline as cli


def worker(directory,queue):
    os.environ['INSTAGRAM_DISCOVERY_LOG_DIR']=directory
    result=log.invoke(lambda:dict(status='completed'),profile='dvcatucsd',headless=True)
    queue.put(result['execution']['run_id'])


class ExecutionLogTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.directory=self.root/'runs'
        self.env=patch.dict(os.environ,{'INSTAGRAM_DISCOVERY_LOG_DIR':str(self.directory),
            'INSTAGRAM_PAID_REQUEST_LEDGER_DIR':str(self.root/'daily'),
            'INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS':'1','INSTAGRAM_DAILY_COVER_MAX_REQUESTS':'1'})
        self.env.start();self.addCleanup(self.env.stop)

    def read(self,result):
        return json.loads((self.directory/f"{result['execution']['run_id']}.json").read_text())

    def test_initial_record_precedes_work_and_success_is_finalized(self):
        def execute():
            files=list(self.directory.glob('*.json'));self.assertEqual(len(files),1)
            initial=json.loads(files[0].read_text())
            self.assertEqual(initial['status'],'running');self.assertIsNone(initial['ended_at'])
            self.assertEqual(initial['profile'],'dvcatucsd');self.assertTrue(initial['headless'])
            log.begin('scraping');log.finish('scraping',dict(unchanged=8))
            log.begin('screening');log.finish('screening',dict(candidates=0))
            return dict(status='completed',unchanged=8,processed=0,failed=0,review_required=0)
        result=log.invoke(execute,profile='dvcatucsd',headless=True);data=self.read(result)
        self.assertEqual(data['status'],'completed');self.assertIsNotNone(data['ended_at'])
        self.assertEqual(data['stages']['scraping']['unchanged'],8)
        self.assertEqual(data['stages']['gemini']['status'],'not_started')
        self.assertIn('completed',data['summary'])
        self.assertFalse((self.root/'daily').exists())

    def test_reviews_are_distinct_from_success_and_failure(self):
        result=log.invoke(lambda:dict(status='completed',review_required=1,parents=[dict(
            parent_id=459,status='review_required',reason='unanchored_weekday')]))
        data=self.read(result)
        self.assertEqual(data['status'],'completed_with_review_required')
        self.assertEqual(data['parents'][0]['reason'],'unanchored_weekday')

    def test_budget_skip_and_remaining_allowance_are_retained(self):
        from app.parent_pipeline import daily_budget
        ledger=daily_budget.Ledger();ledger.reserve('gemini',1)
        def execute():
            try:ledger.reserve('gemini',2)
            except daily_budget.BudgetStopped:pass
            return dict(status='completed',skipped=1,daily_budget=ledger.summary(),parents=[dict(
                parent_id=2,status='skipped',reason='daily_gemini_budget_exhausted')])
        data=self.read(log.invoke(execute))
        self.assertEqual(data['status'],'completed')
        self.assertEqual(data['daily_budget']['providers']['gemini'],dict(limit=1,used=1,remaining=0))
        self.assertEqual(data['daily_budget']['blocked_reason'],'daily_gemini_budget_exhausted')
        self.assertEqual(data['paid_requests_started']['gemini'],0)

    def test_scrape_authentication_failure_prevents_parent_work(self):
        with patch.object(integration,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(integration,'advisory_lock',return_value=contextlib.nullcontext(True)), \
             patch('app.instagram_scraper.sync_ins.main',return_value=dict(status='failed',scrape_health=dict(
                 status='failed',profiles=[dict(profile='dvcatucsd',reason='login_required',http_status_codes=[403])]))), \
             patch.object(integration,'candidates') as candidates,patch.object(integration,'run_parent') as parent:
            result=log.invoke(lambda:integration.execute(profile='dvcatucsd',run=True),profile='dvcatucsd')
        data=self.read(result)
        self.assertEqual(data['status'],'failed_before_paid_requests')
        item=data['stages']['scraping']['items'][0]
        self.assertEqual(item['health_reasons'],['login_required']);self.assertEqual(item['http_status_codes'],[403])
        self.assertEqual(item['status'],'failed');candidates.assert_not_called();parent.assert_not_called()

    def test_dependency_exception_is_sanitized(self):
        def execute():raise ModuleNotFoundError('SECRET API key /private/path')
        result=log.invoke(execute);data=self.read(result)
        self.assertEqual(result['reason'],'dependency_missing')
        self.assertEqual(data['status'],'failed_before_paid_requests')
        self.assertEqual(data['failure']['reason'],'dependency_missing')
        self.assertNotIn('SECRET',json.dumps(data));self.assertNotIn('/private',json.dumps(data))

    def test_error_after_paid_dispatch_is_distinct(self):
        def execute():
            log.begin('gemini',460);log.paid_request_started('gemini')
            raise RuntimeError('SECRET Gemini body')
        result=log.invoke(execute);data=self.read(result)
        self.assertEqual(data['status'],'failed_after_paid_request')
        self.assertEqual(data['paid_requests_started']['gemini'],1)
        self.assertEqual(data['stages']['gemini']['status'],'failed')
        self.assertNotIn('SECRET',json.dumps(data))

    def test_interruption_and_timeout_are_finalized_without_retry(self):
        for error,expected in ((KeyboardInterrupt(),'interrupted'),(TimeoutError('SECRET'),'timed_out')):
            invoked=Mock(side_effect=error)
            def execute():log.begin('scraping');return invoked()
            result=log.invoke(execute);data=self.read(result)
            self.assertEqual(data['status'],expected);self.assertIsNotNone(data['ended_at'])
            invoked.assert_called_once();self.assertNotIn('SECRET',json.dumps(data))

    def test_sigterm_handler_finalizes_and_restores_previous_handler(self):
        previous=signal.getsignal(signal.SIGTERM)
        def execute():
            log.begin('scraping');os.kill(os.getpid(),signal.SIGTERM)
        result=log.invoke(execute)
        self.assertEqual(self.read(result)['status'],'interrupted')
        self.assertEqual(result['reason'],'termination_signal')
        self.assertEqual(signal.getsignal(signal.SIGTERM),previous)

    def test_live_and_unfinished_records_are_distinguished_without_rewriting(self):
        first=log.Record('dvcatucsd',True)
        try:
            first.begin('scraping')
            second=log.Record('dvcatucsd',True)
            try:self.assertIn(first.run_id,second.data['active_run_ids'])
            finally:second.close()
        finally:first.close()  # Model an uncatchable process exit: no finalize.
        original=(self.directory/f'{first.run_id}.json').read_bytes()
        next_result=log.invoke(lambda:dict(status='completed'))
        self.assertIn(first.run_id,self.read(next_result)['previous_unfinished_run_ids'])
        self.assertEqual((self.directory/f'{first.run_id}.json').read_bytes(),original)

    def test_concurrent_processes_have_independent_atomic_records(self):
        context=multiprocessing.get_context('fork');queue=context.Queue()
        processes=[context.Process(target=worker,args=(str(self.directory),queue)) for _ in range(4)]
        try:
            for process in processes:process.start()
            ids=[queue.get(timeout=10) for _ in processes]
            self.assertEqual(len(set(ids)),4)
            for process in processes:process.join(timeout=10);self.assertEqual(process.exitcode,0)
            for run_id in ids:
                data=json.loads((self.directory/f'{run_id}.json').read_text())
                self.assertEqual(data['status'],'completed');self.assertIsNotNone(data['ended_at'])
        finally:
            for process in processes:
                if process.is_alive():process.terminate();process.join()
            queue.close()

    def test_unknown_payloads_and_console_output_are_not_logged(self):
        def execute():
            print('SECRET COOKIE');print('SECRET KEY',file=__import__('sys').stderr)
            log.begin('cover',3)
            log.parent_finished(dict(parent_id=3,status='failed',reason='SECRET',description='SECRET PROMPT',stages={
                'cover':dict(failed=1,reason='SECRET',url='https://signed/SECRET',diagnostics=dict(
                    http_status=503,response_body='SECRET',exception_class='SECRET',response_received=True))}))
            return dict(status='failed',failed=1,response_body='SECRET',reason='SECRET')
        with contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()) as err:
            result=log.invoke(execute)
        self.assertNotIn('SECRET',json.dumps(self.read(result)))
        self.assertEqual(out.getvalue(),'');self.assertEqual(err.getvalue(),'')

    def test_initial_logging_failure_blocks_execution(self):
        execute=Mock()
        with patch.object(log.Record,'save',side_effect=log.LogUnavailable()):result=log.invoke(execute)
        execute.assert_not_called();self.assertEqual(result['reason'],'execution_log_unavailable')

    def test_checkpoint_failure_prevents_dispatch_and_preserves_unfinished_record(self):
        provider=Mock()
        def execute():
            log.begin('gemini',460)
            with patch.object(log.os,'replace',side_effect=OSError('SECRET disk error')):
                log.paid_request_started('gemini')
                provider()
            return dict(status='completed')
        result=log.invoke(execute)
        provider.assert_not_called();self.assertEqual(result['reason'],'execution_log_unavailable')
        unfinished=self.read(result)
        self.assertEqual(unfinished['status'],'running');self.assertIsNone(unfinished['ended_at'])
        following=log.invoke(lambda:dict(status='completed'))
        self.assertIn(unfinished['run_id'],self.read(following)['previous_unfinished_run_ids'])
        self.assertNotIn('SECRET',json.dumps(result))

    def test_busy_execution_has_its_own_record_without_work(self):
        result=log.invoke(lambda:dict(status='busy',reason='integration_already_running'))
        data=self.read(result)
        self.assertEqual(data['status'],'busy')
        self.assertEqual(data['paid_requests_started'],dict(gemini=0,openai_cover=0))

    def test_cli_preflight_creates_no_log_and_run_has_json_and_human_summary(self):
        with patch('dotenv.load_dotenv'),patch.object(integration,'execute',return_value=dict(status='completed',failed=0)), \
             contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(['--profile','dvcatucsd','--headless','--preflight']),0)
            self.assertFalse(self.directory.exists())
            self.assertEqual(cli.main(['--profile','dvcatucsd','--headless','--run']),0)
        result=json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(self.read(result)['headless'],True)
        self.assertIn(result['execution']['run_id'],err.getvalue())

    def test_missing_dotenv_is_recorded_before_pipeline_import(self):
        original=__import__('builtins').__import__
        def importing(name,*args,**kwargs):
            if name=='dotenv':raise ModuleNotFoundError('SECRET missing dependency')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=importing),patch.object(integration,'execute') as execute, \
             contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--profile','dvcatucsd','--run']),1)
        result=json.loads(out.getvalue());data=self.read(result)
        self.assertEqual(data['failure']['reason'],'dependency_missing');execute.assert_not_called()
        self.assertNotIn('SECRET',out.getvalue())

    def test_all_parent_stage_outcomes_are_recorded(self):
        def execute():
            for name in ('gemini','original_image','cover','event_master'):log.begin(name,460)
            log.parent_finished(dict(parent_id=460,status='completed',stages={
                'gemini':dict(attempted=1,succeeded=1),
                'original_image':dict(skipped=1,reason='already_uploaded'),
                'cover':dict(skipped=1,reason='already_generated'),
                'event_master':dict(attempted=1,succeeded=1)}))
            return dict(status='completed',processed=1)
        data=self.read(log.invoke(execute))
        for name in ('gemini','original_image','cover','event_master'):
            item=data['stages'][name]['items'][0]
            self.assertIsNotNone(item['started_at']);self.assertIsNotNone(item['ended_at'])
            self.assertIn(item['status'],('completed','skipped'))

    def test_mocked_real_gemini_boundary_is_recorded_once_on_503(self):
        from test_instagram_gemini_http_budget import GeminiHttpBudgetTests
        from test_instagram_event_extraction import ga
        error=ga.ServerError(503,{'error':{'code':503,'message':'SECRET provider body'}})
        with GeminiHttpBudgetTests().runner([error]) as (m,client,constructor,fetch,persist), \
             patch.object(integration,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(integration,'advisory_lock',return_value=contextlib.nullcontext(True)), \
             patch('app.instagram_scraper.sync_ins.main',return_value=dict(status='completed',profiles=[])), \
             patch('app.parent_pipeline.screening.eligible',return_value=dict(status='eligible',reason='explicit_future_event_date')), \
             patch.object(integration,'candidates',return_value=[dict(parent_id=460,eligible=True,reason='eligible')]):
            result=log.invoke(lambda:integration.execute(profile='dvcatucsd',run=True))
            data=self.read(result)
            self.assertEqual(data['status'],'failed_after_paid_request')
            self.assertEqual(data['paid_requests_started']['gemini'],1)
            self.assertEqual(data['stages']['gemini']['items'][0]['diagnostics']['http_status'],503)
            self.assertEqual(data['daily_budget']['providers']['gemini']['used'],1)
            client.models.generate_content.assert_called_once();persist.assert_not_called()
            self.assertNotIn('SECRET',json.dumps(data))

    def test_cover_artifact_reuse_does_not_report_a_new_paid_dispatch(self):
        from test_instagram_post_cover import ParentCoverTests
        fixture=ParentCoverTests();fixture.setUp()
        try:
            fixture.upload.side_effect=RuntimeError('SECRET upload failure')
            def generate():
                log.begin('cover',6)
                fixture.run_cover(allow_generation=True)
            first=log.invoke(generate)
            self.assertEqual(self.read(first)['paid_requests_started']['openai_cover'],1)
            self.assertEqual(self.read(first)['status'],'failed_after_paid_request')
            fixture.upload.side_effect=None
            def resume():
                log.begin('cover',6)
                fixture.run_cover(allow_generation=False)
                log.finish('cover',dict(attempted=1,succeeded=1),6)
                return dict(status='completed')
            second=log.invoke(resume)
            self.assertEqual(self.read(second)['status'],'completed')
            self.assertEqual(self.read(second)['paid_requests_started']['openai_cover'],0)
            fixture.edit.assert_called_once()
        finally:fixture.doCleanups()
