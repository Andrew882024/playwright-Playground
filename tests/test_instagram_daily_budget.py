"""Disposable ledgers and mocked providers only; no live state or network."""
import contextlib
import json
import multiprocessing
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_instagram_event_extraction import ga, row_six
from app.parent_pipeline import daily_budget as daily, attempt_safety, service, integration


def reserve_worker(directory, barrier, queue):
    ledger=daily.Ledger(directory,dict(gemini=1,openai_cover=1))
    barrier.wait()
    try:
        ledger.reserve('gemini',1)
        queue.put('reserved')
    except daily.BudgetStopped as exc:
        queue.put(exc.reason)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.directory=Path(tmp.name)/'durable'
        self.ledger=daily.Ledger(self.directory,dict(gemini=1,openai_cover=1))

    def test_readonly_preview_creates_nothing(self):
        summary=self.ledger.summary()
        self.assertEqual(summary['providers']['gemini'],dict(limit=1,used=0,remaining=1))
        self.assertFalse(self.directory.exists())

    def test_default_limits_and_invalid_configuration(self):
        with patch.dict(daily.os.environ,{},clear=True):
            self.assertEqual(daily.Ledger(self.directory).limits,dict(gemini=1,openai_cover=1))
        for value in ('-1','SECRET invalid'):
            with patch.dict(daily.os.environ,{'INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS':value}):
                with self.assertRaisesRegex(daily.BudgetStopped,'^daily_paid_ledger_unavailable$'):
                    daily.Ledger(self.directory)

    def test_restart_and_separate_provider_limits(self):
        self.ledger.reserve('gemini',460)
        restarted=daily.Ledger(self.directory,dict(gemini=1,openai_cover=1))
        with self.assertRaisesRegex(daily.BudgetStopped,'daily_gemini_budget_exhausted'):
            restarted.reserve('gemini',461)
        restarted.reserve('openai_cover',460)
        self.assertEqual(restarted.summary()['providers']['openai_cover']['used'],1)
        self.assertEqual(len(json.loads((self.directory/'ledger.json').read_text())['days']),1)

    def test_concurrent_processes_reserve_exactly_once(self):
        self.ledger.reserve('openai_cover',100)  # Initialize durable state before the concurrency race.
        ctx=multiprocessing.get_context('fork')
        barrier=ctx.Barrier(4);queue=ctx.Queue()
        processes=[ctx.Process(target=reserve_worker,args=(str(self.directory),barrier,queue)) for _ in range(4)]
        try:
            for process in processes: process.start()
            outcomes=[queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode,0)
            self.assertEqual(outcomes.count('reserved'),1)
            self.assertEqual(outcomes.count('daily_gemini_budget_exhausted'),3)
            self.assertEqual(self.ledger.summary()['providers']['gemini']['used'],1)
        finally:
            for process in processes:
                if process.is_alive(): process.terminate();process.join()
            queue.close()

    def test_local_midnight_and_dst_rollover(self):
        # Spring LA midnight=08:00 UTC, next midnight=07:00 UTC (23h day).
        # Autumn LA midnight=07:00 UTC, next midnight=08:00 UTC (25h day).
        for before,after in (('2026-03-08T07:59:59+00:00','2026-03-08T08:00:00+00:00'),
                             ('2026-03-09T06:59:59+00:00','2026-03-09T07:00:00+00:00'),
                             ('2026-11-01T06:59:59+00:00','2026-11-01T07:00:00+00:00'),
                             ('2026-11-02T07:59:59+00:00','2026-11-02T08:00:00+00:00')):
            with tempfile.TemporaryDirectory() as tmp:
                ledger=daily.Ledger(tmp,dict(gemini=1,openai_cover=1))
                with patch.object(daily,'now',return_value=datetime.fromisoformat(before)):
                    ledger.reserve('gemini',1)
                    self.assertEqual(ledger.summary()['providers']['gemini']['remaining'],0)
                with patch.object(daily,'now',return_value=datetime.fromisoformat(after)):
                    ledger.reserve('gemini',2)
                    self.assertEqual(ledger.summary()['providers']['gemini']['used'],1)

    def test_dst_clock_repetition_does_not_reset_budget(self):
        with patch.object(daily,'now',return_value=datetime.fromisoformat('2026-11-01T08:30:00+00:00')):
            self.ledger.reserve('gemini',1)  # 01:30 PDT
        with patch.object(daily,'now',return_value=datetime.fromisoformat('2026-11-01T09:30:00+00:00')):
            with self.assertRaisesRegex(daily.BudgetStopped,'daily_gemini_budget_exhausted'):
                self.ledger.reserve('gemini',2)  # 01:30 PST, same day

    def test_limits_cannot_be_raised_during_initialized_day(self):
        self.ledger.reserve('gemini',1)
        higher=daily.Ledger(self.directory,dict(gemini=100,openai_cover=100))
        with self.assertRaisesRegex(daily.BudgetStopped,'daily_gemini_budget_exhausted'):
            higher.reserve('gemini',2)
        self.assertEqual(higher.summary()['providers']['gemini']['limit'],1)

    def test_zero_limit_does_not_reserve(self):
        ledger=daily.Ledger(self.directory,dict(gemini=0,openai_cover=0))
        with self.assertRaises(daily.BudgetStopped):ledger.reserve('gemini',1)
        self.assertEqual(ledger.summary()['providers']['gemini']['used'],0)

    def test_corruption_missing_ledger_and_write_failure_fail_closed(self):
        self.ledger.reserve('gemini',1)
        path=self.directory/'ledger.json'
        for value in ('SECRET invalid json',None):
            if value is None:path.unlink()
            else:path.write_text(value)
            with self.assertRaisesRegex(daily.BudgetStopped,'daily_paid_ledger_unavailable'):
                self.ledger.reserve('openai_cover',1)
            self.assertEqual(self.ledger.summary()['status'],'unavailable')
        # No corruption is reset, even by a fresh Ledger instance.
        self.assertTrue((self.directory/'ledger.lock').exists());self.assertFalse(path.exists())

    def test_unwritable_ledger_denies_requests(self):
        with patch.object(self.ledger,'_save',side_effect=OSError('SECRET disk')):
            with self.assertRaisesRegex(daily.BudgetStopped,'daily_paid_ledger_unavailable'):
                self.ledger.reserve('gemini',1)

    def test_read_failure_denies_and_does_not_expose_private_error(self):
        self.ledger.reserve('gemini',1)
        with patch('builtins.open',side_effect=PermissionError('SECRET private path')):
            with self.assertRaisesRegex(daily.BudgetStopped,'^daily_paid_ledger_unavailable$'):
                self.ledger.reserve('openai_cover',1)
            self.assertEqual(self.ledger.summary(),dict(status='unavailable',reason='daily_paid_ledger_unavailable'))

    def test_lock_timeout_denies_requests(self):
        with patch.object(daily.fcntl,'flock',side_effect=BlockingIOError()), \
             patch.object(daily.time,'monotonic',side_effect=[0,3]):
            with self.assertRaisesRegex(daily.BudgetStopped,'daily_paid_ledger_unavailable'):
                self.ledger.reserve('gemini',1)


class DispatchTests(unittest.TestCase):
    def runner(self,response):
        from test_instagram_gemini_http_budget import GeminiHttpBudgetTests
        return GeminiHttpBudgetTests().runner(response)

    def test_503_timeout_and_interruption_remain_counted(self):
        for error in (ga.ServerError(503,{'error':{'code':503}}),TimeoutError('SECRET'),KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__),self.runner(error) as (m,client,constructor,fetch,persist):
                if isinstance(error,KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):service.run(460)
                else:service.run(460)
                ledger=daily.Ledger()
                self.assertEqual(ledger.summary()['providers']['gemini']['used'],1)
                client.models.generate_content.assert_called_once()
                service.run(460)
                client.models.generate_content.assert_called_once()
                self.assertEqual(ledger.summary()['providers']['gemini']['used'],1)
                persist.assert_not_called()

    def test_budget_denial_has_no_intent_commit_or_permanent_failure(self):
        with self.runner(SimpleNamespace(text=json.dumps(row_six()))) as (m,client,constructor,fetch,persist):
            with patch.dict(daily.os.environ,{'INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS':'1'}):
                ledger=daily.Ledger();ledger.reserve('gemini',100)
                result=service.run(460)
                self.assertEqual(result['status'],'skipped')
                self.assertEqual(result['reason'],'daily_gemini_budget_exhausted')
                self.assertEqual(result['stages']['gemini']['failed'],0)
                self.assertEqual(result['gemini_http_requests'],0)
                self.assertFalse(attempt_safety.inspect(460)['record_exists'])
                self.assertFalse(m.parent.ai_analyzed)
                client.models.generate_content.assert_not_called();persist.assert_not_called()
                self.assertEqual(result['daily_budget']['providers']['gemini']['remaining'],0)

    def test_ledger_failure_blocks_dispatch_without_leaking_error(self):
        with self.runner(SimpleNamespace(text=json.dumps(row_six()))) as (m,client,constructor,fetch,persist), \
             patch.object(daily.Ledger,'_save',side_effect=OSError('SECRET credentials')):
            result=service.run(460)
            self.assertEqual(result['status'],'review_required')
            self.assertEqual(result['reason'],'daily_paid_ledger_unavailable')
            self.assertNotIn('SECRET',json.dumps(result))
            client.models.generate_content.assert_not_called();persist.assert_not_called()
            self.assertFalse(attempt_safety.inspect(460)['record_exists'])

    def test_reservation_is_durable_before_sdk_dispatch(self):
        def respond(**kwargs):
            self.assertEqual(daily.Ledger().summary()['providers']['gemini']['used'],1)
            return SimpleNamespace(text=json.dumps(row_six()))
        with self.runner(respond):
            result=service.run(460)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['daily_budget']['providers']['gemini']['used'],1)

    def test_manual_retry_cannot_bypass_daily_limit_or_erase_history(self):
        error=ga.ServerError(503,{'error':{'code':503}})
        with self.runner([error,SimpleNamespace(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            with patch.dict(daily.os.environ,{'INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS':'1'}):
                service.run(460)
                path=attempt_safety.DIRECTORY/'460.json';before=path.read_bytes()
                result=service.run(460,authorize_gemini_retry=True,confirm_gemini_retry=True)
                self.assertEqual(result['reason'],'daily_gemini_budget_exhausted')
                self.assertEqual(path.read_bytes(),before)
                client.models.generate_content.assert_called_once();persist.assert_not_called()

    def test_midnight_does_not_retry_an_uncertain_parent(self):
        with self.runner(TimeoutError('SECRET')) as (m,client,constructor,fetch,persist):
            with patch.object(daily,'now',return_value=datetime.fromisoformat('2026-11-05T07:59:00+00:00')):
                service.run(460)
            with patch.object(daily,'now',return_value=datetime.fromisoformat('2026-11-05T08:01:00+00:00')):
                result=service.run(460)
                self.assertEqual(result['status'],'review_required')
                self.assertEqual(result['daily_budget']['providers']['gemini']['used'],0)
            client.models.generate_content.assert_called_once()

    def test_strict_zero_http_limit_creates_no_daily_reservation(self):
        with self.runner(SimpleNamespace(text=json.dumps(row_six()))) as (m,client,constructor,fetch,persist):
            result=service.run(460,gemini_max_http_requests=0)
            self.assertEqual(result['daily_budget']['providers']['gemini']['used'],0)
            self.assertFalse(daily.Ledger().directory.exists())
            client.models.generate_content.assert_not_called()

    def test_preflight_only_reads_daily_usage(self):
        with self.runner([]) as (m,client,constructor,fetch,persist):
            result=service.preflight(460)
            self.assertEqual(result['daily_budget']['providers']['gemini']['remaining'],100)
            self.assertFalse(daily.Ledger().directory.exists())
            client.models.generate_content.assert_not_called()

    def test_discovery_and_standalone_share_reservations(self):
        with self.runner(SimpleNamespace(text=json.dumps(row_six()))) as (m,client,constructor,fetch,persist):
            with patch.dict(daily.os.environ,{'INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS':'1'}):
                service.run(460)
                # This fake discovery boundary reaches the real shared ledger, not the provider.
                def parent(parent_id,**kwargs):
                    try:daily.reserve('gemini',parent_id)
                    except daily.BudgetStopped as exc:
                        return dict(status='skipped',reason=exc.reason,parent_id=parent_id,stages={})
                with patch.object(integration,'configured_profiles',return_value=['dvcatucsd']), \
                     patch.object(integration,'advisory_lock',return_value=contextlib.nullcontext(True)), \
                     patch('app.instagram_scraper.sync_ins.main',return_value=dict(status='completed',profiles=[])), \
                     patch.object(integration,'candidates',return_value=[dict(parent_id=461,eligible=True,reason='eligible')]), \
                     patch.object(integration,'run_parent',side_effect=parent):
                    result=integration.execute(profile='dvcatucsd',run=True)
                self.assertEqual(result['status'],'completed');self.assertEqual(result['skipped'],1)
                self.assertEqual(result['parents'][0]['reason'],'daily_gemini_budget_exhausted')
                self.assertEqual(result['daily_budget']['providers']['gemini']['used'],1)
                client.models.generate_content.assert_called_once()

    def test_cover_paid_failure_and_artifact_reuse(self):
        from test_instagram_post_cover import ParentCoverTests
        fixture=ParentCoverTests();fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger=daily.Ledger(tmp,dict(gemini=1,openai_cover=1))
                with daily.capture(ledger):
                    fixture.upload.side_effect=RuntimeError('offline upload')
                    with self.assertRaises(RuntimeError):fixture.run_cover(allow_generation=True)
                    self.assertEqual(ledger.summary()['providers']['openai_cover']['used'],1)
                    fixture.upload.side_effect=None
                    self.assertEqual(fixture.run_cover(allow_generation=False)['mode'],'resumed')
                    self.assertEqual(ledger.summary()['providers']['openai_cover']['used'],1)
                    fixture.edit.assert_called_once()
        finally:fixture.doCleanups()

    def test_cover_budget_denial_stays_prepared_and_uncertain_requests_stay_counted(self):
        from test_instagram_post_cover import ParentCoverTests
        for uncertain in (False,True):
            fixture=ParentCoverTests();fixture.setUp()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ledger=daily.Ledger(tmp,dict(gemini=1,openai_cover=1))
                    with daily.capture(ledger):
                        if uncertain:fixture.edit.side_effect=TimeoutError('SECRET')
                        else:ledger.reserve('openai_cover',100)
                        with self.assertRaises(RuntimeError):
                            fixture.run_cover(allow_generation=True)
                        self.assertEqual(ledger.summary()['providers']['openai_cover']['used'],1)
                        _,manifest=fixture.manifest()
                        self.assertEqual(manifest['state'],'request_started' if uncertain else 'prepared')
                        if uncertain:
                            with self.assertRaises(RuntimeError):fixture.run_cover(allow_generation=True)
                            fixture.edit.assert_called_once()
                        else:fixture.edit.assert_not_called()
                        self.assertNotIn('SECRET',json.dumps(manifest))
            finally:fixture.doCleanups()

    def test_cover_budget_denial_does_not_block_fallback_import(self):
        with self.runner([]) as (m,client,constructor,fetch,persist):
            m.parent.ai_analyzed=True
            m.cover.side_effect=daily.BudgetStopped('daily_openai_cover_budget_exhausted')
            result=service.run(460)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['stages']['cover']['skipped'],1)
            self.assertEqual(result['stages']['cover']['reason'],'daily_openai_cover_budget_exhausted')
            m.sync.assert_called_once_with([460])
