"""Offline model selection tests; journals live only in temporary fixtures."""
import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import Mock,patch

from test_instagram_event_extraction import serial,ga,row_six
from app.parent_pipeline import service,attempt_safety
from app.ai_analyze_service.model_selection import select_chain
import run_instagram_parent_pipeline as cli

MODEL='gemini-3-flash-preview'


class ModelOverrideTests(unittest.TestCase):
    def runner(self,responses):
        from test_instagram_gemini_http_budget import GeminiHttpBudgetTests
        return GeminiHttpBudgetTests().runner(responses)

    def journal(self):return json.loads((attempt_safety.DIRECTORY/'460.json').read_text())

    def test_singleton_override_does_not_read_or_mutate_default_chain(self):
        defaults=Mock(return_value=['default','fallback'])
        self.assertEqual(select_chain(defaults,MODEL,parent_id=460),[MODEL])
        defaults.assert_not_called()
        self.assertEqual(select_chain(defaults),['default','fallback'])
        defaults.assert_called_once()
        with self.assertRaises(ValueError):select_chain(defaults,MODEL)

    def test_selected_model_durable_before_dispatch_and_in_success_diagnostics(self):
        def response(**kwargs):
            request=self.journal()['requests'][-1]
            self.assertEqual(request['state'],'intent')
            self.assertEqual(request['model'],MODEL)
            self.assertEqual(kwargs['model'],MODEL)
            return type('Response',(),{'text':json.dumps(row_six())})()
        with self.runner(response) as (m,client,constructor,fetch,persist), \
             patch.object(serial,'gemini_model_chain') as defaults:
            result=service.run(460,gemini_model=MODEL,gemini_max_http_requests=1)
            defaults.assert_not_called()
            self.assertEqual(result['gemini_model_override'],MODEL)
            self.assertFalse(result['gemini_automatic_model_fallback'])
            self.assertEqual(result['stages']['gemini']['selected_model'],MODEL)
            self.assertEqual(result['gemini_http_requests'],1)
            self.assertEqual(self.journal()['requests'][-1]['model'],MODEL)
            self.assertEqual(self.journal()['requests'][-1]['state'],'committed')
            client.models.generate_content.assert_called_once()
            self.assertEqual(constructor.call_args.kwargs['http_options']['retry_options'],{'attempts':1})
            self.assertEqual(fetch.call_args.kwargs['post_id'],460)

    def test_errors_and_json_failures_never_fall_back_or_retry(self):
        cases=[ga.ServerError(503,{'error':{'code':503,'message':'PRIVATE_TEST'}}),
               ga.ClientError(429,{'error':{'code':429}}),TimeoutError('PRIVATE_TEST'),
               type('Response',(),{'text':'{}'})()]
        for failure in cases:
            with self.subTest(failure=type(failure).__name__), self.runner([failure]) as (m,client,constructor,fetch,persist):
                result=service.run(460,gemini_model=MODEL,gemini_max_http_requests=1)
                client.models.generate_content.assert_called_once()
                self.assertEqual(client.models.generate_content.call_args.kwargs['model'],MODEL)
                self.assertEqual(result['gemini_http_requests'],1)
                self.assertEqual(result['stages']['gemini']['diagnostics']['selected_model'],MODEL)
                self.assertTrue(attempt_safety.review_required(460))
                persist.assert_not_called();m.cover.assert_not_called();m.sync.assert_not_called()
                self.assertNotIn('PRIVATE_TEST',json.dumps(result)+json.dumps(self.journal()))

    def test_switching_model_does_not_bypass_failed_record_or_retry_confirmation(self):
        error=ga.ServerError(503,{'error':{'code':503}})
        with self.runner([error,type('Response',(),{'text':json.dumps(row_six())})()]) as (m,client,constructor,fetch,persist):
            service.run(460,gemini_model=MODEL,gemini_max_http_requests=1)
            failed=copy.deepcopy(self.journal())
            before=(attempt_safety.DIRECTORY/'460.json').read_bytes()
            result=service.run(460,gemini_model='gemini-2.5-flash',gemini_max_http_requests=1)
            self.assertEqual(result['gemini_http_requests'],0)
            self.assertEqual((attempt_safety.DIRECTORY/'460.json').read_bytes(),before)
            client.models.generate_content.assert_called_once()
            for flags in ({'authorize_gemini_retry':True},{'confirm_gemini_retry':True}):
                with self.assertRaises(ValueError):service.run(460,gemini_model=MODEL,**flags)
            result=service.run(460,gemini_model='gemini-2.5-flash',gemini_max_http_requests=1,
                               authorize_gemini_retry=True,confirm_gemini_retry=True)
            self.assertEqual(result['gemini_http_requests'],1)
            data=self.journal()
            self.assertEqual(data['requests'][0],failed['requests'][0])
            self.assertEqual(data['requests'][1]['model'],'gemini-2.5-flash')
            self.assertTrue(data['requests'][1]['manual_retry_authorized'])
            self.assertEqual(len(data['requests']),2)

    def test_retry_and_large_budget_still_dispatches_only_one_request(self):
        error=ga.ServerError(503,{'error':{'code':503}})
        with self.runner([error,error]) as (m,client,constructor,fetch,persist):
            service.run(460,gemini_model=MODEL,gemini_max_http_requests=1)
            result=service.run(460,gemini_model=MODEL,gemini_max_http_requests=3,
                               authorize_gemini_retry=True,confirm_gemini_retry=True)
            self.assertEqual(result['gemini_http_requests'],1)
            self.assertEqual(client.models.generate_content.call_count,2)
            self.assertEqual(len(self.journal()['requests']),2)

    def test_zero_budget_creates_no_journal_request(self):
        with self.runner([]) as (m,client,constructor,fetch,persist):
            result=service.run(460,gemini_model=MODEL,gemini_max_http_requests=0)
            self.assertEqual(result['gemini_http_requests'],0)
            client.models.generate_content.assert_not_called()
            self.assertFalse((attempt_safety.DIRECTORY/'460.json').exists())

    def test_override_does_not_leak_to_later_default_run_or_environment(self):
        original=dict(os.environ)
        response=type('Response',(),{'text':json.dumps(row_six())})()
        with self.runner([response]) as (m,client,constructor,fetch,persist):
            service.run(460,gemini_model=MODEL,gemini_max_http_requests=1)
        with self.runner([response]) as (m,client,constructor,fetch,persist):
            service.run(460,gemini_max_http_requests=1)
            self.assertEqual(client.models.generate_content.call_args.kwargs['model'],'first')
        self.assertEqual(dict(os.environ),original)

    def test_cli_preflight_run_and_invalid_model_safety(self):
        with patch('dotenv.load_dotenv'),patch.object(service,'run',return_value=dict(status='completed')) as run, \
             patch.object(service,'preflight',return_value=dict(status='preflight')) as preflight, \
             contextlib.redirect_stdout(io.StringIO()) as output,contextlib.redirect_stderr(io.StringIO()):
            args=['--parent-id','460','--gemini-model',MODEL,'--gemini-max-http-requests','1']
            self.assertEqual(cli.main(args+['--preflight']),0)
            run.assert_not_called();preflight.assert_called_once_with(460)
            self.assertIn(MODEL,output.getvalue())
            for bad in ('https://example.com/?key=PRIVATE_TEST','gemini-3-flash-preview\nPRIVATE_TEST','', 'Bearer secret'):
                with self.assertRaises(SystemExit):cli.main(['--parent-id','460','--gemini-model',bad,'--run'])
            for flag in ('--authorize-gemini-retry','--confirm-gemini-retry'):
                with self.assertRaises(SystemExit):cli.main(args+['--run',flag])
            run.assert_not_called()
            self.assertEqual(cli.main(args+['--run','--authorize-gemini-retry','--confirm-gemini-retry']),0)
            run.assert_called_once_with(460,gemini_model=MODEL,gemini_max_http_requests=1,
                                       authorize_gemini_retry=True,confirm_gemini_retry=True)
