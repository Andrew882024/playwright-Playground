"""20/3 configuration regressions; disposable ledgers only."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from app.parent_pipeline import daily_budget as daily
import test_instagram_daily_budget as budget_tests
from test_instagram_event_extraction import ga
from app.parent_pipeline import service, attempt_safety

LIMITS = dict(gemini=20, openai_cover=3)
SETTINGS = dict(INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS='20', INSTAGRAM_DAILY_COVER_MAX_REQUESTS='3')


class DailyPilotLimitTests(unittest.TestCase):
    def test_documented_settings_and_provider_limits(self):
        cfg = dotenv_values(Path(__file__).resolve().parents[1]/'.env.example')
        self.assertEqual({key: cfg[key] for key in SETTINGS}, SETTINGS)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(daily.os.environ, SETTINGS):
            ledger = daily.Ledger(tmp)
            self.assertEqual(ledger.limits, LIMITS)
            for provider, maximum in LIMITS.items():
                for index in range(maximum):
                    ledger.reserve(provider, index+1)
                with self.assertRaisesRegex(daily.BudgetStopped, f'daily_{provider}_budget_exhausted'):
                    ledger.reserve(provider, 100)
                self.assertEqual(ledger.summary()['providers'][provider], dict(limit=maximum, used=maximum, remaining=0))
            restarted = daily.Ledger(tmp)
            self.assertEqual(restarted.summary()['providers'], ledger.summary()['providers'])

    def test_existing_day_stays_pinned_and_next_la_day_uses_new_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = daily.Ledger(tmp, dict(gemini=1, openai_cover=1))
            with patch.object(daily, 'now', return_value=datetime(2026,11,6,7,59,tzinfo=timezone.utc)):
                old.reserve('gemini', 1645)
                before = (Path(tmp)/'ledger.json').read_bytes()
                new = daily.Ledger(tmp, LIMITS)
                self.assertEqual(new.summary()['providers']['gemini'], dict(limit=1, used=1, remaining=0))
                self.assertEqual(new.summary()['providers']['openai_cover'], dict(limit=1, used=0, remaining=1))
                self.assertEqual((Path(tmp)/'ledger.json').read_bytes(), before)
            with patch.object(daily, 'now', return_value=datetime(2026,11,6,8,1,tzinfo=timezone.utc)):
                self.assertEqual(new.summary()['providers']['gemini'], dict(limit=20, used=0, remaining=20))
                self.assertEqual(new.summary()['providers']['openai_cover'], dict(limit=3, used=0, remaining=3))
                new.reserve('gemini', 1646)
            import json
            days = json.loads((Path(tmp)/'ledger.json').read_text())['days']
            self.assertEqual(len(days), 2)
            self.assertEqual(days['2026-11-05']['gemini']['reservations'][0]['parent_id'], 1645)

    def test_raised_daily_allowance_does_not_retry_503_or_429(self):
        for error in (ga.ServerError(503, {'error': {'code': 503}}), ga.ClientError(429, {'error': {'code': 429}})):
            with self.subTest(code=error.code), budget_tests.DispatchTests().runner(error) as (_, client, _, _, persist), \
                    patch.dict(daily.os.environ, SETTINGS):
                service.run(460, gemini_max_http_requests=1)
                self.assertEqual(client.models.generate_content.call_count, 1)
                self.assertTrue(attempt_safety.review_required(460))
                self.assertEqual(daily.Ledger().summary()['providers']['gemini']['used'], 1)
                again = service.run(460, gemini_max_http_requests=1)
                self.assertEqual(again['status'], 'review_required')
                self.assertEqual(client.models.generate_content.call_count, 1)
                persist.assert_not_called()
