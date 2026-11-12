"""Offline browser/DB mocks only; no live Instagram or paid API requests."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from test_instagram_event_extraction import serial  # installs offline ORM imports
from app.instagram_scraper import sync_ins as scraper
from app.parent_pipeline import integration
import run_instagram_discovery_pipeline as cli


def response(body=None, status=200):
    return Mock(url='https://www.instagram.com/graphql/query', status=status,
                text=Mock(return_value=json.dumps(body or {'data': {
                    'xdt_api__v1__feed__user_timeline_graphql_connection': {'edges': []}}})))


class CaptureTests(unittest.TestCase):
    def capture(self, reply):
        health=scraper.new_capture_health()
        handler,_=scraper._make_graphql_posts_capture([],set(),health)
        handler(reply)
        return health

    def test_explicit_empty_timeline_is_healthy(self):
        health=self.capture(response())
        self.assertEqual(health['valid_timeline_responses'],1)
        self.assertIsNone(scraper.capture_failure(health))

    def test_response_read_failure_is_safe(self):
        reply=response();reply.text.side_effect=RuntimeError('SECRET signed URL cookie')
        health=self.capture(reply)
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')
        self.assertNotIn('SECRET',json.dumps(health))

    def test_http_error_and_invalid_json(self):
        for reply in (response(status=403),response()):
            if reply.status==200:reply.text.return_value='SECRET invalid response'
            health=self.capture(reply)
            self.assertEqual(scraper.capture_failure(health),'response_capture_failed')
            self.assertNotIn('SECRET',json.dumps(health))

    def test_unrelated_or_graphql_error_response_not_valid_timeline(self):
        for body in ({'data':{'inbox':{'edges':[]}}}, {'errors':[{'message':'SECRET'}]}):
            health=self.capture(response(body))
            self.assertIsNotNone(scraper.capture_failure(health))
            self.assertEqual(health['valid_timeline_responses'],0)
            self.assertNotIn('SECRET',json.dumps(health))


class ProfileHealthTests(unittest.TestCase):
    def scrape(self, *, url='https://www.instagram.com/dvcatucsd/', status=200,
               reply=True, login=False, challenge=False, navigation_error=False, request_failed=False):
        page=MagicMock();page.url=url
        page.locator.side_effect=lambda selector:Mock(count=Mock(return_value=int(
            login if 'username' in selector else challenge)))
        handlers={}
        page.on.side_effect=lambda name,handler:handlers.update({name:handler})
        def navigate(*args,**kwargs):
            if navigation_error:raise TimeoutError('SECRET signed URL')
            if request_failed:handlers['requestfailed'](Mock(url='https://www.instagram.com/graphql/query?SECRET'))
            if reply:handlers['response'](response() if reply is True else reply)
            return Mock(status=status)
        page.goto.side_effect=navigate
        def upsert(user,posts,*,report,inserted_parent_ids):
            report.update(inserted=0,updated=0,unchanged=len(posts));return len(posts)
        with patch.object(scraper,'known_shortcodes_for_profile',return_value=set()), \
             patch.object(scraper,'upsert_posts_for_profile',side_effect=upsert) as write, \
             patch.object(scraper,'MAX_PROFILE_SCROLLS',0),contextlib.redirect_stdout(io.StringIO()):
            result=scraper._scroll_profile_capture_and_upsert(page,'dvcatucsd','https://www.instagram.com/dvcatucsd/')
        self.assertEqual(page.remove_listener.call_count,2)
        return result,write

    def test_login_redirect_and_login_form_block_writes(self):
        for kwargs in ({'url':'https://www.instagram.com/accounts/login/?SECRET'}, {'login':True}):
            result,write=self.scrape(**kwargs)
            self.assertEqual(result['scrape_health']['reason'],'login_required')
            write.assert_not_called();self.assertNotIn('SECRET',json.dumps(result))

    def test_challenge_redirect_and_form_block_writes(self):
        for kwargs in ({'url':'https://www.instagram.com/challenge/SECRET'}, {'challenge':True}):
            result,write=self.scrape(**kwargs)
            self.assertEqual(result['scrape_health']['reason'],'authentication_challenge')
            write.assert_not_called()

    def test_blocked_navigation_timeout_and_no_capture(self):
        for kwargs,reason in (({'status':429},'navigation_blocked'),
                              ({'navigation_error':True},'navigation_or_browser_failed'),
                              ({'reply':False},'no_valid_timeline_response'),
                              ({'request_failed':True},'response_capture_failed'),
                              ({'reply':response(status=403)},'response_capture_failed')):
            result,write=self.scrape(**kwargs)
            self.assertEqual(result['scrape_health']['reason'],reason)
            write.assert_not_called();self.assertNotIn('SECRET',json.dumps(result))

    def test_verified_empty_profile_is_successful_zero(self):
        result,write=self.scrape()
        self.assertEqual(result['scrape_health']['status'],'healthy')
        self.assertEqual(result['scrape_health']['captured_posts'],0)
        self.assertEqual(result['inserted'],0);write.assert_called_once()

    def test_all_existing_posts_is_legitimate_zero_new(self):
        reply=response({'data':{'timeline_connection':{'edges':[{'node':{
            'code':'existing', 'taken_at':1793750400}}]}}})
        with patch.object(scraper,'recent_post',return_value=True):
            result,write=self.scrape(reply=reply)
        self.assertEqual(result['scrape_health']['status'],'healthy')
        self.assertEqual(result['unchanged'],1);self.assertEqual(result['inserted'],0)


class HeadlessIntegrationTests(unittest.TestCase):
    def test_firefox_headed_default_and_headless_opt_in(self):
        for args,headless in (([],False),(['--headless'],True)):
            playwright=MagicMock()
            launch=playwright.return_value.__enter__.return_value.firefox.launch_persistent_context
            launch.return_value.pages=[MagicMock()]
            with patch.object(scraper,'TARGET_URLS',['dvcatucsd']), \
                 patch.object(scraper,'load_instagram_cookies_for_playwright',return_value=[]), \
                 patch.object(scraper,'sync_playwright',playwright), \
                 patch.object(scraper,'SCREENSHOT_DIR',MagicMock()), \
                 patch.object(scraper,'PERSISTENT_PROFILE_DIR',MagicMock()), \
                 patch.object(scraper,'_scroll_profile_capture_and_upsert',return_value=dict(profile='dvcatucsd',
                     scrape_health=dict(status='healthy',reason='timeline_verified'))) as profile, \
                 contextlib.redirect_stdout(io.StringIO()):
                result=scraper.main(args)
            self.assertEqual(launch.call_args.kwargs['headless'],headless)
            self.assertEqual(result['scrape_health']['status'],'healthy')
            profile.assert_called_once()

    def execute(self, scraped, headless=False):
        with patch.object(integration,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(integration,'advisory_lock',return_value=contextlib.nullcontext(True)) as lock, \
             patch('app.instagram_scraper.sync_ins.main',return_value=scraped) as scrape, \
             patch.object(integration,'candidates',return_value=[]) as candidates, \
             patch.object(integration,'run_parent') as parent:
            result=integration.execute(profile='dvcatucsd',run=True,headless=headless)
        lock.assert_called_once_with(integration.RUN_LOCK,1)
        parent.assert_not_called()
        return result,scrape,candidates

    def test_failed_scrape_stops_before_candidates_and_paid_work(self):
        health=dict(status='failed',profiles=[dict(profile='dvcatucsd',reason='login_required')])
        result,scrape,candidates=self.execute(dict(status='failed',scrape_health=health))
        self.assertEqual(result['status'],'failed');self.assertEqual(result['scrape_health'],health)
        self.assertEqual(result['gemini_attempts'],0);self.assertEqual(result['cover_attempts'],0)
        candidates.assert_not_called()

    def test_headless_forwarded_and_zero_new_success(self):
        result,scrape,candidates=self.execute(dict(status='completed',unchanged=8,
            scrape_health=dict(status='healthy',profiles=[])),headless=True)
        scrape.assert_called_once_with(['--profile','dvcatucsd','--headless'])
        self.assertEqual(result['status'],'completed');self.assertEqual(result['unchanged'],8)

    def test_cli_headless_preflight_and_failed_exit(self):
        for args,expected in (([],False),(['--headless'],True)):
            with patch('dotenv.load_dotenv'),patch.object(integration,'execute',return_value=dict(status='failed',
                    scrape_health=dict(status='failed'))) as execute,contextlib.redirect_stdout(io.StringIO()) as output:
                code=cli.main(['--profile','dvcatucsd',*args])
            self.assertEqual(execute.call_args.kwargs['headless'],expected)
            self.assertFalse(execute.call_args.kwargs['run'])
            self.assertEqual(code,1);self.assertEqual(json.loads(output.getvalue())['scrape_health']['status'],'failed')

    def test_scraper_exception_is_structured_and_never_reaches_paid_work(self):
        with patch.object(integration,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(integration,'advisory_lock',return_value=contextlib.nullcontext(True)), \
             patch('app.instagram_scraper.sync_ins.main',side_effect=RuntimeError('SECRET cookie URL')), \
             patch.object(integration,'candidates') as candidates,patch.object(integration,'run_parent') as parent:
            result=integration.execute(profile='dvcatucsd',run=True)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['scrape_health']['reason'],'scrape_execution_failed')
        self.assertNotIn('SECRET',json.dumps(result))
        candidates.assert_not_called();parent.assert_not_called()
