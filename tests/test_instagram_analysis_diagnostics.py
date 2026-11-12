"""Offline phase/request instrumentation and redaction regressions."""
import contextlib
import io
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as Row
from unittest.mock import Mock,patch
from test_instagram_event_extraction import serial,ga,row_six,TZ
from app.ai_analyze_service import diagnostics as diagnostic
from app.parent_pipeline import service


class DiagnosticTests(unittest.TestCase):
    def analyze(self,phase=None):
        details=diagnostic.new_diagnostics();stats=dict(attempted=0,committed=0,failed=0)
        client=Mock();client.models.generate_content.return_value=Row(text=json.dumps(row_six()))
        secret='SECRET_TEST https://cdn.example/image?signature=SECRET_TEST'
        with ExitStack() as stack:
            stack.enter_context(patch.object(serial,'fetch_instagram_rows_for_profile',return_value=[Row(id=1669,post_shortcode='offline')]))
            prepare=stack.enter_context(patch.object(serial,'build_analyzer_post_dict',return_value={'shortcode':'offline'}))
            urls=stack.enter_context(patch.object(ga,'_image_urls_from_post',return_value=[]))
            fetch=stack.enter_context(patch.object(ga,'_fetch_image',return_value=(b'fake','image/jpeg')))
            persist=stack.enter_context(patch.object(serial,'persist_analysis',return_value=True))
            stack.enter_context(patch.object(ga.time,'sleep'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            if phase=='input_preparation':prepare.side_effect=ValueError(secret)
            if phase=='image_retrieval':urls.return_value=['hidden-reference'];fetch.side_effect=RuntimeError(secret)
            if phase=='gemini_request':client.models.generate_content.side_effect=ga.ClientError(401,{'error':{'code':401,'message':secret}})
            if phase=='response_parsing_validation':client.models.generate_content.return_value=Row(text=secret)
            if phase=='database_persistence':persist.side_effect=RuntimeError(secret)
            with diagnostic.capture(details):
                serial.process_profile_db('offline',client=client,model_chain=['offline-model'],tz=TZ,
                    limit=1,only_not_analyzed=True,sleep_s=0,analysis_stats=stats)
        return details,stats,client

    def test_each_failure_phase_is_explicit_and_sensitive_data_is_absent(self):
        for phase in ('input_preparation','image_retrieval','gemini_request','response_parsing_validation','database_persistence'):
            with self.subTest(phase=phase):
                details,stats,client=self.analyze(phase)
                self.assertEqual(details['failure_phase'],phase)
                self.assertEqual(stats['attempted'],1);self.assertEqual(stats['failed'],1)
                self.assertFalse(details['extraction_committed'])
                self.assertNotIn('SECRET_TEST',json.dumps(details))
                self.assertNotIn('https://',json.dumps(details))
                self.assertTrue(details['exception_class'])
                self.assertEqual(details['api_request_started'],phase not in ('input_preparation','image_retrieval'))
                self.assertEqual(details['response_received'],phase in ('gemini_request','response_parsing_validation','database_persistence'))
                if phase=='gemini_request':
                    self.assertEqual(details['http_status'],401)
                    self.assertEqual(client.models.generate_content.call_count,1)

    def test_success_is_committed_and_request_response_instrumented(self):
        details,stats,client=self.analyze()
        self.assertTrue(details['extraction_committed'])
        self.assertTrue(details['response_received']);self.assertEqual(details['request_count'],1)
        self.assertIsNone(details['failure_phase']);self.assertEqual(stats['committed'],1)

    def test_ambiguous_transport_failure_is_not_marked_as_response_or_retried(self):
        details=diagnostic.new_diagnostics();client=Mock()
        client.models.generate_content.side_effect=TimeoutError('SECRET_TEST')
        with diagnostic.capture(details),self.assertRaises(TimeoutError):
            ga._generate_content_with_429_retry(client,'offline',[],None)
        self.assertTrue(details['api_request_started']);self.assertFalse(details['response_received'])
        self.assertEqual(client.models.generate_content.call_count,1)
        self.assertNotIn('SECRET_TEST',json.dumps(details))

    def test_image_http_failure_is_recorded_without_url(self):
        from urllib.error import HTTPError
        details=diagnostic.new_diagnostics()
        with diagnostic.capture(details),patch.object(ga,'urlopen',side_effect=HTTPError('https://secret.invalid',403,'SECRET_TEST',{},None)):
            diagnostic.phase('image_retrieval');self.assertIsNone(ga._fetch_image('https://secret.invalid/a.jpg'))
        self.assertEqual(details['events'][0]['http_status'],403)
        self.assertFalse(details['api_request_started']);self.assertFalse(details['response_received'])
        self.assertNotIn('secret.invalid',json.dumps(details))

    def test_cli_failure_json_contains_structured_capture_not_console_text(self):
        parent=Row(ai_analyzed=False,profile_username='offline')
        def fail(*a,**kwargs):
            diagnostic.phase('gemini_request');diagnostic.event('request_started')
            diagnostic.event('failure',TimeoutError('SECRET_TEST'))
            kwargs['analysis_stats'].update(attempted=1,committed=0,failed=1)
            print('SECRET_TEST signed-source-url')
        with patch.object(service,'parent_lock',return_value=contextlib.nullcontext(True)),patch.object(service,'read_parent',return_value=(parent,[])),patch.object(serial,'main',side_effect=fail):
            result=service.run(1669)
        details=result['stages']['gemini']['diagnostics']
        self.assertEqual(details['failure_phase'],'gemini_request')
        self.assertFalse(details['extraction_committed'])
        self.assertEqual(result['stages']['cover']['reason'],'extraction_not_committed')
        self.assertNotIn('SECRET_TEST',json.dumps(result))
