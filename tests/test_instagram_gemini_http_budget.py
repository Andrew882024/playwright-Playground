"""Offline strict generation budgets, including real serial-analyzer control flow."""
import contextlib
import io
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as Row
from unittest.mock import Mock,patch

from test_instagram_event_extraction import serial,ga,row_six,TZ
from app.ai_analyze_service import http_budget,diagnostics
from app.parent_pipeline import service
import run_instagram_parent_pipeline as cli


class GeminiHttpBudgetTests(unittest.TestCase):
    def analyze(self,side_effect,limit=1):
        client=Mock();client.models.generate_content.side_effect=side_effect
        budget=http_budget.GeminiHttpBudget(limit)
        with http_budget.capture(budget),patch.object(ga.time,'sleep'), \
             contextlib.redirect_stderr(io.StringIO()):
            try:
                outcome=ga.analyze_post(client,['first','second'],{},TZ)
            except http_budget.GeminiRequestStopped:
                outcome=None
        return client,budget,outcome

    def test_limit_one_blocks_all_transient_quota_auth_and_uncertain_retries(self):
        errors=[ga.ServerError(code,{'error':{'code':code}}) for code in (500,502,503,504)]
        errors += [ga.ClientError(code,{'error':{'code':code}}) for code in (400,401,403,404,429)]
        errors += [TimeoutError('PRIVATE_TEST_TOKEN'),ConnectionError('PRIVATE_TEST_URL')]
        for error in errors:
            with self.subTest(error=type(error).__name__,code=getattr(error,'code',None)):
                client,budget,outcome=self.analyze([error,Row(text=json.dumps(row_six()))])
                self.assertIsNone(outcome);self.assertEqual(client.models.generate_content.call_count,1)
                self.assertEqual(budget.dispatched,1)
                self.assertEqual(budget.stopped_reason,'gemini_http_budget_exhausted')
                self.assertEqual(client.models.generate_content.call_args.kwargs['model'],'first')

    def test_limit_one_blocks_json_repair(self):
        for invalid in ('not JSON','{}'):
            client,budget,outcome=self.analyze([Row(text=invalid),Row(text=json.dumps(row_six()))])
            self.assertIsNone(outcome);self.assertEqual(client.models.generate_content.call_count,1)
            self.assertEqual(budget.dispatched,1)
            self.assertEqual(budget.stopped_reason,'gemini_http_budget_exhausted')

    def test_limit_two_allows_two_successful_http_responses_for_json_repair(self):
        client,budget,outcome=self.analyze([Row(text='{}'),Row(text=json.dumps(row_six()))],2)
        self.assertIsNotNone(outcome);self.assertEqual(client.models.generate_content.call_count,2)
        self.assertEqual(budget.dispatched,2);self.assertIsNone(budget.stopped_reason)

    def test_strict_mode_never_retries_failure_even_with_remaining_budget(self):
        for error in (TimeoutError('PRIVATE_TEST'),ga.ServerError(503,{'error':{'code':503}})):
            client,budget,outcome=self.analyze([error,Row(text=json.dumps(row_six()))],3)
            self.assertEqual(client.models.generate_content.call_count,1)
            self.assertIn(budget.stopped_reason,('gemini_http_request_failed','gemini_http_outcome_uncertain'))

    def test_zero_budget_dispatches_nothing(self):
        client,budget,outcome=self.analyze([Row(text=json.dumps(row_six()))],0)
        client.models.generate_content.assert_not_called();self.assertEqual(budget.dispatched,0)
        self.assertEqual(budget.stopped_reason,'gemini_http_budget_exhausted')

    def test_default_budget_preserves_fallback_and_counts_all_dispatches(self):
        error=ga.ServerError(503,{'error':{'code':503}})
        client,budget,outcome=self.analyze([error,error,error,Row(text=json.dumps(row_six()))],None)
        self.assertEqual(client.models.generate_content.call_count,4)
        self.assertEqual(budget.dispatched,4);self.assertEqual(outcome[1],1)
        self.assertIsNone(http_budget.current())

    def test_budget_counts_before_dispatch_and_is_shared_between_models(self):
        budget=http_budget.GeminiHttpBudget(1);client=Mock()
        def response(**kwargs):
            self.assertEqual(budget.dispatched,1)
            return 'response'
        client.models.generate_content.side_effect=response
        with http_budget.capture(budget):
            self.assertEqual(ga._generate_content_with_transient_retry(client,'first',[],None),'response')
            with self.assertRaises(http_budget.GeminiRequestStopped):
                ga._generate_content_with_transient_retry(client,'second',[],None)
        client.models.generate_content.assert_called_once()

    def test_real_sdk_with_mock_http_transport_dispatches_once_on_503_or_timeout(self):
        import httpx
        for failure in ('503','timeout'):
            sent=[]
            def handler(request):
                sent.append(request.method)
                if failure=='timeout':raise httpx.ReadTimeout('OFFLINE_TEST',request=request)
                return httpx.Response(503,json={'error':{'code':503,'message':'offline unavailable'}})
            client=ga.genai.Client(api_key='OFFLINE_TEST_KEY',http_options={
                'timeout':120000,'retry_options':{'attempts':1},
                'client_args':{'transport':httpx.MockTransport(handler)}})
            budget=http_budget.GeminiHttpBudget(1)
            try:
                with http_budget.capture(budget),self.assertRaises(http_budget.GeminiRequestStopped):
                    ga.analyze_post(client,['offline-model','offline-fallback'],{},TZ)
            finally:client.close()
            self.assertEqual(sent,['POST']);self.assertEqual(budget.dispatched,1)

    @contextlib.contextmanager
    def runner(self,response):
        from test_instagram_parent_pipeline_cli import ParentPipelineTests
        actual_main=serial.main
        with ParentPipelineTests().mocked(False) as m,ExitStack() as stack:
            m.parent.id=460
            m.analyze.side_effect=actual_main
            client=Mock();client.models.generate_content.side_effect=response
            constructor=stack.enter_context(patch.object(serial.genai,'Client',return_value=client))
            stack.enter_context(patch.object(serial,'gemini_api_key',return_value='OFFLINE_TEST_KEY'))
            stack.enter_context(patch.object(serial,'gemini_model_chain',return_value=['first','second']))
            fetch=stack.enter_context(patch.object(serial,'fetch_instagram_rows_for_profile',return_value=[Row(id=460,post_shortcode='offline')]))
            stack.enter_context(patch.object(serial,'build_analyzer_post_dict',return_value={'shortcode':'offline'}))
            persist=stack.enter_context(patch.object(serial,'persist_analysis',return_value=True))
            stack.enter_context(patch.object(ga.time,'sleep'))
            yield m,client,constructor,fetch,persist

    def test_real_serial_flow_stops_downstream_reports_count_and_disables_sdk_retry(self):
        for response in ([Row(text='{}')], [ga.ServerError(503,{'error':{'code':503}})], [TimeoutError('PRIVATE_TEST')]):
            with self.subTest(response=type(response[0]).__name__),self.runner(response) as (m,client,constructor,fetch,persist):
                result=service.run(460,gemini_max_http_requests=1)
                self.assertEqual(result['status'],'budget_exhausted')
                self.assertEqual(result['reason'],'gemini_http_budget_exhausted')
                self.assertEqual(result['gemini_http_requests'],1)
                self.assertEqual(result['gemini_max_http_requests'],1)
                self.assertFalse(result['stages']['gemini']['diagnostics']['extraction_committed'])
                self.assertEqual(result['stages']['gemini']['diagnostics']['request_count'],1)
                self.assertEqual(constructor.call_args.kwargs['http_options']['retry_options'],{'attempts':1})
                self.assertEqual(fetch.call_args.kwargs['post_id'],460)
                client.models.generate_content.assert_called_once();persist.assert_not_called()
                m.cover.assert_not_called();m.backfill.assert_not_called();m.sync.assert_not_called()
                self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_success_at_limit_commits_and_keeps_existing_cover_import_behavior(self):
        with self.runner([Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            result=service.run(460,gemini_max_http_requests=1)
            self.assertEqual(result['status'],'completed');self.assertEqual(result['gemini_http_requests'],1)
            persist.assert_called_once();m.cover.assert_called_once();m.sync.assert_called_once_with([460])
            self.assertEqual(m.backfill.call_args.kwargs['post_id'],460)

    def test_durable_parent_runner_disables_sdk_retries_without_strict_limit(self):
        with self.runner([Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            result=service.run(460)
            self.assertEqual(result['gemini_http_requests'],1)
            self.assertEqual(constructor.call_args.kwargs['http_options'],{'timeout':120000,'retry_options':{'attempts':1}})

    def test_strict_failure_after_dispatch_preserves_count(self):
        with self.runner([Row(text=json.dumps(row_six()))]) as (m,client,constructor,fetch,persist):
            m.read.side_effect=[(m.parent,m.rows),RuntimeError('PRIVATE_TEST')]
            result=service.run(460,gemini_max_http_requests=1)
            self.assertEqual(result['status'],'failed');self.assertEqual(result['gemini_http_requests'],1)
            self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_cli_option_validation_preflight_and_execution(self):
        with patch('dotenv.load_dotenv'),patch.object(service,'run',return_value=dict(status='completed',gemini_http_requests=1)) as run, \
             patch.object(service,'preflight',return_value=dict(status='preflight')) as preflight, \
             contextlib.redirect_stdout(io.StringIO()) as output,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--parent-id','460','--run','--gemini-max-http-requests','1']),0)
            run.assert_called_once_with(460,gemini_max_http_requests=1)
            self.assertEqual(cli.main(['--parent-id','460','--preflight','--gemini-max-http-requests','1']),0)
            preflight.assert_called_once_with(460)
            for value in ('-1','not-an-integer'):
                with self.assertRaises(SystemExit):cli.main(['--parent-id','460','--gemini-max-http-requests',value])
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['gemini_http_requests'],0)
