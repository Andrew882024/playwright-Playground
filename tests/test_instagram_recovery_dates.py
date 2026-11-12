"""Offline paid-generation activity-date policy tests."""
import contextlib
import io
import json
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as Row
from unittest.mock import patch
from test_instagram_recovery import MemoryStore
from app.pipeline_recovery.activity_dates import generation_policy
from app.pipeline_recovery.service import RecoveryRun


class DatePolicyTests(unittest.TestCase):
    now=datetime(2026,11,3,20,tzinfo=timezone.utc)
    def snapshot(self,activities):return Row(context=json.dumps(dict(activities=activities)))
    def test_expired_future_and_mixed_dates(self):
        expired=dict(start='2026-11-02T17:00:00-08:00',end='2026-11-02T19:00:00-08:00')
        future=dict(start='2026-11-04T17:00:00-08:00',end='2026-11-04T19:00:00-08:00')
        self.assertEqual(generation_policy(self.snapshot([expired]),self.now),'skipped_expired')
        self.assertIsNone(generation_policy(self.snapshot([future]),self.now))
        self.assertIsNone(generation_policy(self.snapshot([expired,future]),self.now))
        self.assertEqual(generation_policy(self.snapshot([dict(end=self.now.isoformat())]),self.now),'skipped_expired')
    def test_missing_naive_and_fallback_dates(self):
        for activities in ([],[{}],[dict(end='bad',start='2026-11-04T17:00:00')]):
            self.assertEqual(generation_policy(self.snapshot(activities),self.now),'activity_date_review_required')
        self.assertIsNone(generation_policy(self.snapshot([dict(end=None,start='2026-11-04T17:00:00-08:00')]),self.now))
        self.assertEqual(generation_policy(self.snapshot([dict(end=None,start='2026-11-01T17:00:00-08:00')]),self.now),'skipped_expired')
    def run_case(self,activities,resume=False):
        run=RecoveryRun(MemoryStore(),1,dict(cover=1,event_master=1),dict(cover=True,event_master=False))
        def cover(*args,**kwargs):
            if not resume:kwargs['generation_guard'](self.snapshot(activities),reserve=True)
            return dict(mode='resumed',api_calls=0)
        with patch('app.event_cover_service.parent_cover.run_parent_cover',side_effect=cover), \
             patch('app.event_cover_service.openai_cover.Settings.from_env'),contextlib.redirect_stdout(io.StringIO()):
            result=run.recover('cover')
        self.assertEqual(run.cover_allowance_used,0)
        return result
    def test_expired_skips_without_paid_budget(self):
        result=self.run_case([dict(end='2000-01-01T00:00:00+00:00')])
        self.assertEqual(result['skipped_expired'],1)
        self.assertEqual(result['parents'][0]['status'],'skipped_expired')
        self.assertEqual(result['failed'],0)
    def test_missing_dates_require_review_but_artifact_recovery_still_allowed(self):
        self.assertEqual(self.run_case([{}])['review_required'],1)
        self.assertEqual(self.run_case([{}],resume=True)['recovered'],1)
        self.assertEqual(self.run_case([dict(end='2000-01-01T00:00:00+00:00')],resume=True)['recovered'],1)
