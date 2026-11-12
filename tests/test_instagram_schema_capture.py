"""Database-free browser-only capture; browser and cookies always mocked."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from unittest.mock import MagicMock,Mock,patch

from test_instagram_event_extraction import serial
from test_instagram_scrape_health import response
from app.instagram_scraper import sync_ins as scraper
from app.instagram_scraper.schema_capture import capture
import diagnose_instagram_graphql_schema as cli


class CaptureOnlyTests(unittest.TestCase):
    def invoke(self,reply=None,interrupted=False,login=False,startup_failure=False,headless=True):
        page=MagicMock();page.url='https://www.instagram.com/dvcatucsd/'
        page.locator.side_effect=lambda selector:Mock(count=Mock(return_value=int(login and 'username' in selector)))
        handlers={};page.on.side_effect=lambda event,callback:handlers.update({event:callback})
        def goto(*args,**kwargs):
            if reply is not None:handlers['response'](reply)
            if interrupted:raise KeyboardInterrupt()
            return Mock(status=200)
        page.goto.side_effect=goto
        browser=MagicMock();context=browser.new_context.return_value
        context.new_page.return_value=page
        runtime=MagicMock();runtime.firefox.launch.return_value=browser
        if startup_failure:runtime.firefox.launch.side_effect=RuntimeError('PRIVATE_BROWSER_ERROR')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(scraper,'sync_playwright',return_value=Mock(
                __enter__=Mock(return_value=runtime),__exit__=Mock(return_value=False))))
            stack.enter_context(patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]))
            stack.enter_context(patch.object(scraper,'MAX_PROFILE_SCROLLS',0))
            forbidden=[stack.enter_context(patch.object(scraper,name,side_effect=AssertionError('forbidden')))
                       for name in ('known_shortcodes_for_profile','upsert_posts_for_profile',
                                    '_scroll_profile_capture_and_upsert','main')]
            result=capture('dvcatucsd',headless=headless)
            runtime.firefox.launch.assert_called_once_with(headless=headless)
            for operation in forbidden:operation.assert_not_called()
        return result,page,browser,context

    def test_successful_capture_has_no_database_calls_or_browser_artifacts(self):
        data={'data':{'timeline_connection':{'edges':[{'node':{'code':'one'}}]}}}
        result,page,browser,context=self.invoke(response(data))
        self.assertEqual(result['status'],'completed');self.assertEqual(result['captured_posts'],1)
        self.assertEqual(result['database_writes'],0)
        self.assertEqual(result['gemini_http_requests'],0);self.assertEqual(result['openai_generation_requests'],0)
        page.screenshot.assert_not_called();context.storage_state.assert_not_called()
        context.close.assert_called_once();browser.close.assert_called_once()
        page.remove_listener.assert_any_call('response',unittest.mock.ANY)
        self.assertNotIn('one',json.dumps(result))

    def test_headed_capture_uses_same_isolated_path(self):
        result,*_=self.invoke(response(),headless=False)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['database_writes'],0)

    def test_unknown_profile_fails_before_browser_or_cookie_access(self):
        with patch.object(scraper,'sync_playwright') as browser, \
             patch.object(scraper,'load_instagram_cookies_for_playwright') as cookies:
            result=capture('not_a_configured_account')
        browser.assert_not_called();cookies.assert_not_called()
        self.assertEqual(result['scrape_health']['reason'],'unknown_configured_profile')

    def test_legitimate_empty_capture_has_no_database_calls(self):
        result,*_=self.invoke(response())
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['scrape_health']['valid_empty_timeline_responses'],1)
        self.assertEqual(result['database_writes'],0)

    def test_failed_capture_has_no_database_calls(self):
        result,*_=self.invoke(response({'errors':[{'message':'PRIVATE_ERROR'}]}))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['scrape_health']['reason'],'response_capture_failed')
        self.assertNotIn('PRIVATE_ERROR',json.dumps(result))

    def test_no_responses_and_auth_failure_remain_fail_closed(self):
        for kwargs,reason in (({},'no_valid_timeline_response'),({'login':True},'login_required')):
            result,*_=self.invoke(**kwargs)
            self.assertEqual(result['status'],'failed');self.assertEqual(result['scrape_health']['reason'],reason)

    def test_interruption_cleans_up_without_database_calls(self):
        result,page,browser,context=self.invoke(response(),interrupted=True)
        self.assertEqual(result['status'],'interrupted');self.assertEqual(result['database_writes'],0)
        context.close.assert_called_once();browser.close.assert_called_once()

    def test_startup_failure_never_falls_back(self):
        result,*_=self.invoke(startup_failure=True)
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('PRIVATE_BROWSER_ERROR',json.dumps(result))

    def test_cli_capture_exception_never_falls_back_or_prints_raw_error(self):
        output=io.StringIO()
        with patch('app.instagram_scraper.schema_capture.capture',side_effect=RuntimeError('PRIVATE_ERROR')) as call, \
             patch('run_instagram_discovery_pipeline.main') as discovery,contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['--profile','dvcatucsd','--capture']),1)
        call.assert_called_once();discovery.assert_not_called()
        self.assertNotIn('PRIVATE_ERROR',output.getvalue())

    def test_cli_suppresses_unstructured_browser_console_output(self):
        def execute(*args,**kwargs):
            print('PRIVATE_STDOUT');print('PRIVATE_STDERR',file=sys.stderr)
            return {'status':'failed','database_writes':0}
        output=io.StringIO();errors=io.StringIO()
        with patch('app.instagram_scraper.schema_capture.capture',side_effect=execute), \
             contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
            cli.main(['--profile','dvcatucsd','--capture'])
        self.assertNotIn('PRIVATE',output.getvalue()+errors.getvalue())

    def test_fresh_process_capture_cannot_import_database_or_downstream_modules(self):
        # Test import isolation in a new interpreter: the suite's mocked ORM in
        # sys.modules must not hide an accidental production dependency.
        program=r'''
import sys,importlib.abc,itertools
attempts=[]
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        forbidden=('Blueprint_db','scrape_db','sqlalchemy','psycopg2',
                   'app.instagram_scraper.scrape_db','app.parent_pipeline','app.ai_analyze_service',
                   'app.event_cover_service','app.event_master_sync','app.pipeline_recovery',
                   'run_instagram_discovery_pipeline','boto3','google.genai','openai')
        if any(fullname==name or fullname.startswith(name+'.') for name in forbidden):
            attempts.append(fullname)
            raise AssertionError('Forbidden dependency')
sys.meta_path.insert(0,Guard())
from unittest.mock import Mock,MagicMock,patch
from app.instagram_scraper import sync_ins as scraper
from app.instagram_scraper.schema_capture import capture
import json
for partial_mode,outcome in itertools.product((False,True),('success','empty','failure','interrupted')):
    page=MagicMock();page.url='https://www.instagram.com/dvcatucsd/'
    page.locator.return_value.count.return_value=0
    handlers={};page.on.side_effect=lambda key,value:handlers.update({key:value})
    body={'data':{'timeline_connection':{'edges':[] if outcome=='empty' else [{'node':{'code':'one'}}]}}}
    if outcome=='failure':body={'errors':[{'message':'PRIVATE'}]}
    def goto(*args,**kwargs):
        handlers['response'](Mock(url='https://www.instagram.com/graphql/query',status=200,
                                 text=Mock(return_value=json.dumps(body))))
        if outcome=='interrupted':raise KeyboardInterrupt()
        return Mock(status=200)
    page.goto.side_effect=goto
    runtime=MagicMock();runtime.firefox.launch.return_value.new_context.return_value.new_page.return_value=page
    with patch.object(scraper,'sync_playwright') as pw,patch.object(scraper,'MAX_PROFILE_SCROLLS',0), \
         patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]):
        pw.return_value.__enter__.return_value=runtime
        result=capture('dvcatucsd',headless=True,partial_capture=partial_mode)
        assert result['database_writes']==0
        expected={'success':'completed','empty':'completed','failure':'failed','interrupted':'interrupted'}[outcome]
        assert result['status']==('partial_unverified' if partial_mode else expected)
        if partial_mode:assert result['capture_outcome']==expected
assert not attempts
print('isolated')
'''
        result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,'Fresh-process isolation check failed')
        self.assertEqual(result.stdout.strip(),'isolated')
