"""Offline cutoff, pinned-grid and safe lifecycle logging regressions."""
import contextlib
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from app.instagram_scraper import recency
import test_instagram_event_post_id as candidate_tests
from app.routes import instagram_pipeline as route


class RecencyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.cutoff = self.now - timedelta(days=40)

    def post(self, code, days, pinned=False):
        return dict(shortcode=code, taken_at=int((self.now-timedelta(days=days)).timestamp()), is_pinned=pinned)

    def test_default_boundary_unknown_and_override(self):
        with patch.dict(os.environ, {'INSTAGRAM_SCRAPE_MAX_AGE_DAYS': ''}):
            self.assertEqual(recency.scrape_cutoff(self.now), self.cutoff)
        self.assertTrue(recency.recent_post(self.post('boundary', 40), self.cutoff))
        self.assertFalse(recency.recent_post(self.post('old', 41), self.cutoff))
        for value in (None, 'invalid', 10**50):
            self.assertFalse(recency.recent_post({'taken_at': value}, self.cutoff))
        with patch.dict(os.environ, {'INSTAGRAM_SCRAPE_MAX_AGE_DAYS': '20'}):
            self.assertEqual(recency.scrape_cutoff(self.now), self.now-timedelta(days=20))
        with patch.dict(os.environ, {'INSTAGRAM_SCRAPE_MAX_AGE_DAYS': '0'}):
            with self.assertRaises(ValueError): recency.scrape_cutoff(self.now)

    def test_initial_old_pins_do_not_stop_newer_posts(self):
        pins = [self.post(str(i), 100, True) for i in range(3)]
        stopper = recency.OldPostStopper([p['shortcode'] for p in pins], self.cutoff)
        self.assertFalse(stopper.observe(pins))
        self.assertFalse(stopper.observe(pins + [self.post('recent', 1)]))
        self.assertFalse(stopper.observe([self.post('later-pin', 100, True)]))

    def test_two_distinct_reliable_old_batches_stop(self):
        stopper = recency.OldPostStopper([], self.cutoff)
        first = [self.post(str(i), 41+i) for i in range(3)]
        self.assertFalse(stopper.observe(first))
        self.assertFalse(stopper.observe(first))  # repeated responses are not evidence
        self.assertTrue(stopper.observe([self.post(str(i), 41+i) for i in range(3,6)]))

    def test_unknown_pin_timestamp_or_out_of_order_does_not_stop(self):
        for changes in ('pin', 'date', 'order'):
            stopper = recency.OldPostStopper([], self.cutoff)
            for batch in range(3):
                posts = [self.post(f'{batch}-{i}',41+i) for i in range(3)]
                if changes == 'pin': posts[0]['is_pinned'] = None
                if changes == 'date': posts[0]['taken_at'] = None
                if changes == 'order': posts.reverse()
                self.assertFalse(stopper.observe(posts))

    def test_fixed_run_cutoff_used_by_both_candidate_queries(self):
        query = candidate_tests.PostIdCandidateTests()
        with recency.run_cutoff(self.cutoff):
            self.assertEqual(recency.active_cutoff(), self.cutoff)
            for discovery in (False, True):
                compiled = query.compiled_query(discovery=discovery)
                dates = [v for v in compiled.params.values() if isinstance(v, datetime)]
                self.assertTrue(dates)
                self.assertTrue(all(v >= self.cutoff for v in dates))

    def test_stage_logs_timing_and_no_exception_message(self):
        console = io.StringIO(); log = io.StringIO()
        with patch.object(route.sys, '__stdout__', console), contextlib.redirect_stdout(log):
            self.assertEqual(route._run_step('offline', lambda: 7), 7)
            with self.assertRaises(RuntimeError):
                route._run_step('offline', lambda: (_ for _ in ()).throw(RuntimeError('secret-fixture')))
        for output in (console.getvalue(), log.getvalue()):
            for marker in ('status=started', 'status=completed', 'status=failed', 'elapsed_seconds='):
                self.assertIn(marker, output)
            self.assertNotIn('secret-fixture', output)

    def test_scraper_filters_old_pins_without_premature_overlap_exit(self):
        import json
        from unittest.mock import Mock
        from app.instagram_scraper import sync_ins as scraper
        page = Mock()
        page.url = 'https://www.instagram.com/offline/'
        page.locator.return_value.count.return_value = 0
        pins = [dict(code=f'pin{i}', taken_at=self.post('', 100)['taken_at'],
                     is_pinned=True) for i in range(3)]
        recent = dict(code='new', display_url='https://cdninstagram.com/offline.jpg', taken_at=self.post('', 1)['taken_at'], is_pinned=False)
        def respond(nodes):
            handler = page.on.call_args.args[1]
            handler(Mock(status=200, url='https://www.instagram.com/graphql',
                         text=Mock(return_value=json.dumps({'data': {'timeline': {'edges': [
                             {'node': n} for n in nodes]}}}))))
        def navigate(*args, **kwargs):
            respond(pins)
            return Mock(status=200)
        page.goto.side_effect = navigate
        def scroll(*a):
            respond([recent])
        with recency.run_cutoff(self.cutoff), \
             patch.object(scraper, 'known_shortcodes_for_profile', return_value={f'pin{i}' for i in range(3)}), \
             patch.object(scraper, 'upsert_posts_for_profile', return_value=1) as upsert, \
             patch.object(scraper, '_scroll_profile_down_once', side_effect=scroll) as scrolling, \
             patch.object(scraper, 'MAX_PROFILE_SCROLLS', 3), \
             contextlib.redirect_stdout(io.StringIO()):
            scraper._scroll_profile_capture_and_upsert(page, 'offline', 'https://www.instagram.com/offline/')
        self.assertEqual(scrolling.call_count, 3)
        self.assertEqual([p['shortcode'] for p in upsert.call_args.args[1]], ['new'])

    def test_s3_timeouts_and_database_timeouts_are_explicit(self):
        from unittest.mock import Mock
        from app.event_cover_service import openai_cover
        with patch('boto3.client') as client:
            openai_cover._s3_client(Mock(region='offline'))
        config = client.call_args.kwargs['config']
        self.assertEqual(config.connect_timeout, 10)
        self.assertEqual(config.read_timeout, 30)
        self.assertEqual(config.retries['total_max_attempts'], 3)
