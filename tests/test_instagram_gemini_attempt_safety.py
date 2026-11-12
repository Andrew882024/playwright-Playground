"""Shared durable Gemini safety. Every journal lives in a temporary directory."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as Row
from unittest.mock import patch

from test_instagram_event_extraction import ga,row_six
from app.parent_pipeline import attempt_safety,service,screening
import run_instagram_parent_pipeline as cli


class DurableGeminiTests(unittest.TestCase):
    def runner(self,response):
        from test_instagram_gemini_http_budget import GeminiHttpBudgetTests
        return GeminiHttpBudgetTests().runner(response)

    def data(self):
        return json.loads((attempt_safety.DIRECTORY/'460.json').read_text())

    def test_intent_durable_before_dispatch_then_committed(self):
        def response(**kwargs):
            data=self.data();request=data['requests'][-1]
            self.assertEqual(data['status'],'in_progress')
            self.assertEqual(request['parent_id'],460);self.assertEqual(request['state'],'intent')
            self.assertIsNotNone(datetime.fromisoformat(request['timestamp']).tzinfo)
            return Row(text=json.dumps(row_six()))
        with self.runner(response) as (m,client,constructor,fetch,persist):
            result=service.run(460,gemini_max_http_requests=1)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(self.data()['status'],'committed')
            self.assertEqual(self.data()['requests'][-1]['state'],'committed')
            self.assertFalse(attempt_safety.review_required(460))
            self.assertEqual(len(self.data()['requests']),1)
            self.assertIsNone(attempt_safety.current())

    def test_503_timeout_and_invalid_response_block_both_runners_and_screening(self):
        for response,state in ((ga.ServerError(503,{'error':{'code':503,'message':'PRIVATE_TEST'}}),'failed'),
                               (TimeoutError('PRIVATE_TEST signed_url'),'uncertain'),
                               (Row(text='{}'),'validation_failed')):
            with self.subTest(state=state),self.runner([response]) as (m,client,constructor,fetch,persist):
                first=service.run(460,gemini_max_http_requests=1)
                self.assertEqual(first['gemini_http_requests'],1)
                self.assertEqual(self.data()['status'],'review_required')
                self.assertEqual(self.data()['requests'][0]['state'],state)
                snapshot=copy.deepcopy(self.data())
                for pending in (False,True):
                    result=service.run(460,pending_only=pending,gemini_max_http_requests=1)
                    self.assertEqual(result['status'],'review_required')
                    self.assertEqual(result['gemini_http_requests'],0)
                self.assertEqual(screening.eligible({'id':460})['reason'],'previous_analysis_outcome_requires_review')
                self.assertEqual(self.data(),snapshot)
                self.assertEqual(client.models.generate_content.call_count,1)
                persist.assert_not_called();m.cover.assert_not_called();m.sync.assert_not_called()
                self.assertNotIn('PRIVATE_TEST',json.dumps(self.data()))

    def test_failed_parent_request_never_automatically_falls_back_without_strict_cap(self):
        error=ga.ServerError(503,{'error':{'code':503}})
        with self.runner([error,Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            result=service.run(460)
            self.assertEqual(result['status'],'review_required')
            self.assertEqual(result['gemini_http_requests'],1)
            client.models.generate_content.assert_called_once()
            self.assertEqual(constructor.call_args.kwargs['http_options']['retry_options'],{'attempts':1})

    def test_interruption_leaves_intent_and_repeated_invocation_is_blocked(self):
        with self.runner(KeyboardInterrupt()) as (m,client,constructor,fetch,persist):
            with self.assertRaises(KeyboardInterrupt):service.run(460,gemini_max_http_requests=1)
            self.assertEqual(self.data()['status'],'in_progress')
            self.assertEqual(self.data()['requests'][0]['state'],'intent')
            result=service.run(460,gemini_max_http_requests=1)
            self.assertEqual(result['status'],'review_required')
            self.assertEqual(result['gemini_http_requests'],0)
            client.models.generate_content.assert_called_once()

    def test_successful_http_with_db_failure_remains_review_required(self):
        with self.runner([Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            persist.side_effect=RuntimeError('PRIVATE_TEST database')
            result=service.run(460,gemini_max_http_requests=1)
            self.assertEqual(result['status'],'review_required')
            self.assertEqual(self.data()['requests'][0]['state'],'response_received')
            self.assertTrue(attempt_safety.review_required(460))
            self.assertEqual(service.run(460,gemini_max_http_requests=1)['gemini_http_requests'],0)
            client.models.generate_content.assert_called_once()

    def test_confirmed_retry_appends_history_and_success_acknowledges_it(self):
        error=ga.ServerError(503,{'error':{'code':503}})
        with self.runner([error,Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            service.run(460,gemini_max_http_requests=1)
            old=copy.deepcopy(self.data())
            for flags in ({'authorize_gemini_retry':True},{'confirm_gemini_retry':True}):
                with self.assertRaises(ValueError):service.run(460,gemini_max_http_requests=1,**flags)
            result=service.run(460,gemini_max_http_requests=1,authorize_gemini_retry=True,confirm_gemini_retry=True)
            self.assertEqual(result['status'],'completed');self.assertEqual(result['gemini_http_requests'],1)
            data=self.data();self.assertEqual(len(data['requests']),2)
            self.assertEqual(data['requests'][0],old['requests'][0])
            self.assertTrue(data['requests'][1]['manual_retry_authorized'])
            self.assertNotEqual(data['requests'][0]['request_id'],data['requests'][1]['request_id'])
            self.assertEqual(data['history'][:len(old['history'])],old['history'])
            self.assertEqual(data['status'],'committed')
            self.assertEqual(client.models.generate_content.call_count,2)
            # A real committed parent skips Gemini on subsequent normal runs.
            m.parent.ai_analyzed=True
            service.run(460,gemini_max_http_requests=1)
            self.assertEqual(client.models.generate_content.call_count,2)
            self.assertEqual(m.sync.call_args.args,([460],))

    def test_retry_authorization_is_one_request_not_permission_for_automatic_retries(self):
        error=ga.ServerError(503,{'error':{'code':503}})
        with self.runner([error,error,Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            service.run(460,gemini_max_http_requests=1)
            result=service.run(460,gemini_max_http_requests=3,authorize_gemini_retry=True,confirm_gemini_retry=True)
            self.assertEqual(result['gemini_http_requests'],1)
            self.assertEqual(client.models.generate_content.call_count,2)
            self.assertEqual(service.run(460,gemini_max_http_requests=1)['status'],'review_required')
            self.assertEqual(len(self.data()['requests']),2)

    def test_journal_failure_prevents_dispatch_and_zero_budget_creates_no_intent(self):
        for zero in (False,True):
            with self.subTest(zero=zero),self.runner([Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
                with patch.object(attempt_safety,'atomic_write',side_effect=OSError('PRIVATE_TEST disk')) as write:
                    result=service.run(460,gemini_max_http_requests=0 if zero else 1)
                self.assertEqual(result['gemini_http_requests'],0)
                client.models.generate_content.assert_not_called()
                self.assertFalse((attempt_safety.DIRECTORY/'460.json').exists())
                if zero:write.assert_not_called()

    def test_legacy_records_are_preserved_and_not_reset_by_retryable_status(self):
        with self.runner([Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            path=attempt_safety.DIRECTORY/'460.json'
            legacy=dict(parent_id=460,status='review_required',legacy_metadata='PRESERVE')
            path.write_text(json.dumps(legacy))
            self.assertEqual(service.run(460,gemini_max_http_requests=1)['status'],'review_required')
            self.assertEqual(json.loads(path.read_text()),legacy)
            service.run(460,gemini_max_http_requests=1,authorize_gemini_retry=True,confirm_gemini_retry=True)
            self.assertEqual(self.data()['legacy_metadata'],'PRESERVE')
            self.assertIn(dict(status='review_required',legacy=True),self.data()['history'])
            attempt_safety.record(460,'retryable')
            self.assertTrue(attempt_safety.review_required(460))
            self.assertEqual(len(self.data()['requests']),1)

    def test_preflight_is_read_only_and_does_not_fabricate_parent_460_history(self):
        with self.runner([]) as (m,client,constructor,fetch,persist):
            result=service.preflight(460)
            self.assertFalse(result['gemini_attempt_safety']['record_exists'])
            self.assertEqual(result['gemini_attempt_safety']['request_count'],0)
            self.assertFalse((attempt_safety.DIRECTORY/'460.json').exists())
            m.analyze.assert_not_called();client.models.generate_content.assert_not_called()

    def test_cli_requires_separate_confirmation_and_does_not_execute_preflight(self):
        with patch('dotenv.load_dotenv'),patch.object(service,'run',return_value=dict(status='completed')) as run, \
             patch.object(service,'preflight',return_value=dict(status='preflight')) as preflight, \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            for flag in ('--authorize-gemini-retry','--confirm-gemini-retry'):
                with self.assertRaises(SystemExit):cli.main(['--parent-id','460','--run',flag])
            run.assert_not_called()
            flags=['--parent-id','460','--gemini-max-http-requests','1',
                   '--authorize-gemini-retry','--confirm-gemini-retry']
            self.assertEqual(cli.main(flags+['--preflight']),0)
            run.assert_not_called();preflight.assert_called_once_with(460)
            self.assertEqual(cli.main(flags+['--run']),0)
            run.assert_called_once_with(460,gemini_max_http_requests=1,
                                       authorize_gemini_retry=True,confirm_gemini_retry=True)

    def test_unreadable_records_block_even_confirmed_retry_without_overwriting(self):
        with self.runner([]) as (m,client,constructor,fetch,persist):
            path=attempt_safety.DIRECTORY/'460.json';path.write_text('PRIVATE_TEST invalid json')
            result=service.run(460,gemini_max_http_requests=1,authorize_gemini_retry=True,confirm_gemini_retry=True)
            self.assertEqual(result['status'],'review_required')
            self.assertEqual(path.read_text(),'PRIVATE_TEST invalid json')
            client.models.generate_content.assert_not_called()
            self.assertNotIn('PRIVATE_TEST',json.dumps(result))
