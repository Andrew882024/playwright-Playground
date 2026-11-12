"""Real shared dispatch controls with fake SDK, image fetch and database reads."""
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.instagram_scraper import analysis_execution as service
from app.instagram_scraper.analysis_batch import analyzer_input
from app.instagram_scraper.collection_staging import atomic, digest, private_dir
from app.parent_pipeline import attempt_safety, daily_budget
from app.ai_analyze_service import gemini_analysis as ga
from test_instagram_daily_images import observed
from test_instagram_event_extraction import row_six
import analyze_instagram_batch as cli


class AnalysisExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'staging'; private_dir(self.root)
        self.media = observed(); self.media['user'] = {'username': 'envisionucsd'}
        self.post = analyzer_input(self.media, 'envisionucsd')
        self.record = dict(parent_id=1645, account='envisionucsd', shortcode=self.media['code'],
                           source_object=digest(self.media), analyzer_input_hash=digest(self.post),
                           uncertainty_reasons=['source_verification_unavailable', 'graphql_errors_unresolved_for_post'],
                           source_verification=dict(source_membership=False, graphql_integrity=False),
                           import_authorized=False, analysis_authorized=False)
        self.batch = digest(dict(version=1, purpose='analysis_only_not_import', records=[self.record]))
        atomic(private_dir(self.root/'objects')/(digest(self.media)+'.json'), self.media)
        atomic(private_dir(self.root/'analysis-batches')/(self.batch+'.json'),
               dict(batch_id=self.batch, candidates=[self.record]))
        self.ledger = daily_budget.Ledger(Path(self.tmp.name)/'ledger', dict(gemini=1, openai_cover=1))
        self.client = Mock(); self.client.models.generate_content.return_value = Mock(text=json.dumps(row_six()))
        self.lock_held = False
        @contextmanager
        def lock(parent):
            self.assertEqual(parent, 1645); self.lock_held = True
            try: yield True
            finally: self.lock_held = False
        self.patches = [patch.object(attempt_safety, 'DIRECTORY', Path(self.tmp.name)/'attempts'),
            patch.object(service, 'parent_lock', side_effect=lock),
            patch.object(service, 'authoritative_state', return_value=dict(status='current')),
            patch.object(service.daily_budget, 'Ledger', return_value=self.ledger),
            patch.object(ga.genai, 'Client', return_value=self.client),
            patch.object(ga, 'gemini_api_key', return_value='OFFLINE_SECRET'),
            patch.object(ga, 'gemini_model_chain', return_value=['first', 'fallback']),
            patch.object(ga, '_fetch_image', return_value=(b'original', 'image/jpeg'))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def run_analysis(self, **kw):
        return service.execute(self.root, self.batch, 1645, **kw)

    def test_preflight_is_default_no_sdk_or_journal_or_output(self):
        result = self.run_analysis()
        self.assertEqual(result['status'], 'preflight'); self.client.models.generate_content.assert_not_called()
        self.assertFalse((self.root/'analysis-results').exists())
        self.assertFalse(attempt_safety.inspect(1645)['record_exists'])
        self.assertEqual(self.ledger.summary()['providers']['gemini']['used'], 0)

    def test_success_local_only_and_repeat_reuses_without_payment(self):
        def response(**kwargs):
            self.assertTrue(self.lock_held)
            self.assertEqual(attempt_safety.inspect(1645)['last_request_state'], 'intent')
            self.assertEqual(self.ledger.summary()['providers']['gemini']['used'], 1)
            return Mock(text=json.dumps(row_six()))
        self.client.models.generate_content.side_effect = response
        result = self.run_analysis(run=True)
        self.assertEqual(result['status'], 'analyzed_locally'); self.assertEqual(result['gemini_http_requests'], 1)
        saved = json.loads(Path(result['result_path']).read_text())
        self.assertEqual(len(saved['analysis']['events']), 2)
        self.assertFalse(saved['import_authorized']); self.assertFalse(saved['extraction_committed'])
        self.assertFalse(saved['source_authenticity_verified'])
        self.assertEqual(saved['uncertainty_reasons'], self.record['uncertainty_reasons'])
        self.assertEqual(Path(result['result_path']).stat().st_mode & 0o077, 0)
        self.assertTrue(attempt_safety.review_required(1645))
        again = self.run_analysis(run=True)
        self.assertEqual(again['status'], 'reused_local_result'); self.assertEqual(again['gemini_http_requests'], 0)
        self.client.models.generate_content.assert_called_once()
        opts = ga.genai.Client.call_args.kwargs['http_options']
        self.assertEqual(opts['retry_options'], {'attempts': 1})

    def test_first_original_and_caption_are_required_and_unchanged(self):
        result = self.run_analysis(run=True)
        self.assertEqual(result['status'], 'analyzed_locally')
        call = self.client.models.generate_content.call_args.kwargs
        self.assertIn(self.post['caption'], call['contents'][-1])
        self.assertEqual(call['contents'][0].inline_data.data, b'original')
        ga._fetch_image.assert_any_call(self.post['first_image_url'])

    def test_missing_first_image_stops_before_dispatch(self):
        ga._fetch_image.return_value = None
        result = self.run_analysis(run=True)
        self.assertEqual(result['gemini_http_requests'], 0)
        self.assertEqual(result['diagnostics']['failure_phase'], 'image_retrieval')
        self.client.models.generate_content.assert_not_called()
        self.assertFalse(attempt_safety.inspect(1645)['record_exists'])

    def test_empty_first_image_does_not_dispatch(self):
        ga._fetch_image.return_value = (b'', 'image/jpeg')
        self.assertEqual(self.run_analysis(run=True)['gemini_http_requests'], 0)
        self.client.models.generate_content.assert_not_called()

    def test_busy_parent_never_dispatches(self):
        @contextmanager
        def busy(parent):
            yield False
        with patch.object(service, 'parent_lock', side_effect=busy):
            result = self.run_analysis(run=True)
        self.assertEqual(result['reason'], 'parent_busy')
        service.authoritative_state.assert_not_called()
        self.client.models.generate_content.assert_not_called()

    def test_503_timeout_and_interruption_are_durable_and_never_retried(self):
        errors = [ga.ServerError(503, {'error': {'code': 503, 'message': 'PRIVATE_URL'}}),
                  TimeoutError('PRIVATE_URL'), KeyboardInterrupt()]
        for error in errors:
            # Each subcase uses an isolated new temporary journal, never resets a record.
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(attempt_safety, 'DIRECTORY', Path(tmp)/'journal'):
                # Independent fake clock-day budget for each request test.
                self.ledger.directory = Path(tmp)/'ledger'
                self.client.models.generate_content.reset_mock()
                self.client.models.generate_content.side_effect = error
                result = self.run_analysis(run=True)
                self.assertEqual(result['gemini_http_requests'], 1)
                self.assertTrue(attempt_safety.review_required(1645))
                again = self.run_analysis(run=True)
                self.assertEqual(again['reason'], 'previous_analysis_outcome_requires_review')
                self.client.models.generate_content.assert_called_once()
                self.assertNotIn('PRIVATE_URL', json.dumps(result))

    def test_invalid_json_does_not_dispatch_repair(self):
        self.client.models.generate_content.return_value = Mock(text='not JSON SECRET')
        result = self.run_analysis(run=True)
        self.assertEqual(result['gemini_http_requests'], 1)
        self.client.models.generate_content.assert_called_once()
        self.assertTrue(attempt_safety.review_required(1645))

    def test_daily_exhaustion_does_not_create_parent_intent(self):
        self.ledger.reserve('gemini', 999)
        result = self.run_analysis(run=True)
        self.assertEqual(result['reason'], 'daily_gemini_budget_exhausted')
        self.assertFalse(attempt_safety.inspect(1645)['record_exists'])
        self.client.models.generate_content.assert_not_called()

    def test_authoritative_state_rechecked_after_image_download(self):
        service.authoritative_state.side_effect = [dict(status='current'), service.Blocked('already_analyzed')]
        result = self.run_analysis(run=True)
        self.assertEqual(result['reason'], 'already_analyzed')
        self.client.models.generate_content.assert_not_called()
        self.assertEqual(service.authoritative_state.call_count, 2)

    def test_tampered_staged_object_or_batch_is_rejected(self):
        path = self.root/'objects'/(digest(self.media)+'.json')
        value = dict(self.media, caption={'text': 'tampered'}); atomic(path, value)
        result = self.run_analysis(run=True)
        self.assertEqual(result['reason'], 'staged_object_changed')
        self.client.models.generate_content.assert_not_called()

    def test_unbound_parent_is_never_substituted(self):
        result = service.execute(self.root, self.batch, 17, run=True)
        self.assertEqual(result['reason'], 'parent_binding_missing_or_ambiguous')
        service.authoritative_state.assert_not_called(); self.client.models.generate_content.assert_not_called()

    def test_output_failure_keeps_paid_attempt_protected(self):
        original = service.atomic
        def write(path, value):
            if path.name == 'result.json': raise OSError('PRIVATE_SENSITIVE_PATH')
            return original(path, value)
        with patch.object(service, 'atomic', side_effect=write):
            result = self.run_analysis(run=True)
        self.assertEqual(result['status'], 'review_required')
        self.assertTrue(attempt_safety.review_required(1645)); self.client.models.generate_content.assert_called_once()
        self.assertNotIn('PRIVATE_SENSITIVE_PATH', json.dumps(result))

    def test_finalization_holds_parent_lock(self):
        original = attempt_safety.Attempt.finish
        def finish(attempt, **kwargs):
            self.assertTrue(self.lock_held); return original(attempt, **kwargs)
        with patch.object(attempt_safety.Attempt, 'finish', finish):
            self.assertEqual(self.run_analysis(run=True)['status'], 'analyzed_locally')

    def test_cli_requires_separate_confirmation_and_defaults_to_preflight(self):
        args = ['--batch-id', self.batch, '--parent-id', '1645']
        with patch.object(service, 'execute', return_value={'status': 'preflight'}) as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
            self.assertFalse(execute.call_args.kwargs['run'])
        with self.assertRaises(SystemExit): cli.main(args + ['--run'])
        with patch.object(service, 'execute', return_value={'status': 'analyzed_locally'}) as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args + ['--run', '--confirm-one-paid-request']), 0)
            self.assertTrue(execute.call_args.kwargs['run'])

class AuthoritativeReadTests(unittest.TestCase):
    def check(self, *, analyzed=False, caption_changed=False, duplicate=False, children=False, imported=False):
        # ORM definitions are imported through the existing offline engine fixture.
        from types import SimpleNamespace
        import Blueprint_db
        now = datetime.now(timezone.utc)
        post = dict(taken_at=int(now.timestamp()), caption='Join our workshop tomorrow at 6 PM at UCSD.')
        record = dict(account='envisionucsd', shortcode='synthetic')
        parent = SimpleNamespace(id=1645, profile_username='envisionucsd', ai_analyzed=analyzed,
            posted_time=None, posted_unix_seconds=post['taken_at'],
            caption='changed' if caption_changed else post['caption'])
        db = Mock(); db.__enter__ = Mock(return_value=db); db.__exit__ = Mock(return_value=False)
        db.scalars.return_value.all.return_value = [parent, parent] if duplicate else [parent]
        db.scalar.return_value = 1 if children else None
        db.get.return_value = SimpleNamespace(acknowledged_revision='revision') if imported else None
        with patch.object(Blueprint_db, 'SessionLocal', return_value=db), \
                patch('app.parent_pipeline.screening.screen', return_value={'status': 'eligible'}):
            try:
                result = service.authoritative_state(1645, record, post)
            except service.Blocked as exc:
                result = str(exc)
        self.assertEqual(str(db.execute.call_args_list[0].args[0]),
                         'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        for name in ('add', 'delete', 'flush', 'commit'):
            getattr(db, name).assert_not_called()
        return result

    def test_authoritative_read_is_read_only(self):
        self.assertEqual(self.check()['status'], 'verified_current_unanalyzed')

    def test_analyzed_duplicate_changed_extraction_and_ack_are_rejected(self):
        for kwargs, reason in [({'analyzed': True}, 'already_analyzed'),
                ({'duplicate': True}, 'canonical_parent_missing_or_ambiguous'),
                ({'caption_changed': True}, 'authoritative_input_revision_changed'),
                ({'children': True}, 'existing_extraction_requires_review'),
                ({'imported': True}, 'already_imported_acknowledgment')]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.check(**kwargs), reason)
