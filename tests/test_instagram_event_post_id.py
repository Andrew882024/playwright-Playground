"""Offline SQL compilation and CLI routing checks for serial --post-id."""
import contextlib
import io
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy.dialects import postgresql

from test_instagram_event_extraction import serial, TZ
from app.ai_analyze_service import candidates


class PostIdCandidateTests(unittest.TestCase):
    def compiled_query(self, discovery=False, **kwargs):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.execute.return_value.all.return_value = [('acm.at.ucsd',)]
        session.execute.return_value.scalars.return_value.all.return_value = []
        with patch.object(candidates, 'SessionLocal', factory):
            if discovery:
                candidates.distinct_profile_usernames_recent(**kwargs)
            else:
                candidates.fetch_instagram_rows_for_profile('@acm.at.ucsd', **kwargs)
        return session.execute.call_args.args[0].compile(dialect=postgresql.dialect())

    def test_post_id_and_profile_are_both_required_in_sql(self):
        compiled = self.compiled_query(post_id=6)
        sql = str(compiled)
        self.assertIn('instagram_posts.id = %(id_1)s', sql)
        self.assertEqual(compiled.params['id_1'], 6)
        self.assertIn('instagram_posts.profile_username =', sql)
        self.assertIn('acm.at.ucsd', compiled.params.values())
        self.assertIn('coalesce(', sql)
        self.assertIn('>=', sql)
        self.assertIn('instagram_posts.ai_analyzed IS false', sql)

    def test_reanalysis_keeps_id_and_recent_window(self):
        for discovery in (False, True):
            compiled = self.compiled_query(discovery=discovery, post_id=6, only_not_analyzed=False)
            sql = str(compiled)
            self.assertEqual(compiled.params['id_1'], 6)
            self.assertIn('instagram_posts.id =', sql)
            self.assertIn('coalesce(', sql)
            self.assertIn('>=', sql)
            self.assertNotIn('ai_analyzed IS false', sql)

    def test_discovery_restricts_id_and_pending_status(self):
        sql = str(self.compiled_query(discovery=True, post_id=6))
        self.assertIn('instagram_posts.id =', sql)
        self.assertIn('instagram_posts.ai_analyzed IS false', sql)

    def test_omitting_post_id_preserves_old_query(self):
        for discovery in (False, True):
            compiled = self.compiled_query(discovery=discovery)
            self.assertNotIn('id_1', compiled.params)
            self.assertIn('coalesce(', str(compiled))


class PostIdCliTests(unittest.TestCase):
    @contextlib.contextmanager
    def mocked_cli(self):
        with ExitStack() as stack:
            mocks = {}
            for name, value in (
                ('gemini_api_key', 'offline-key'), ('gemini_model_chain', ['offline-model']),
                ('default_tz', TZ), ('distinct_profile_usernames_recent', ['acm.at.ucsd']),
                ('process_profile_db', 0),
            ):
                mocks[name] = stack.enter_context(patch.object(serial, name, return_value=value))
            mocks['client'] = stack.enter_context(patch.object(serial.genai, 'Client'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            yield mocks

    def test_id_without_profile_discovery_and_processing_both_filtered(self):
        with self.mocked_cli() as mocks:
            serial.main(['--post-id', '6', '--reanalyze', '--limit', '1'])
        mocks['distinct_profile_usernames_recent'].assert_called_once_with(only_not_analyzed=False, post_id=6)
        call = mocks['process_profile_db'].call_args
        self.assertEqual(call.args, ('acm.at.ucsd',))
        self.assertEqual(call.kwargs['post_id'], 6)
        self.assertEqual(call.kwargs['limit'], 1)
        self.assertFalse(call.kwargs['only_not_analyzed'])
        mocks['process_profile_db'].assert_called_once()

    def test_profile_and_post_id_are_forwarded_together(self):
        with self.mocked_cli() as mocks:
            serial.main(['--post-id', '6', '--profile', '@another.profile'])
        mocks['distinct_profile_usernames_recent'].assert_not_called()
        call = mocks['process_profile_db'].call_args
        self.assertEqual(call.args, ('another.profile',))
        self.assertEqual(call.kwargs['post_id'], 6)
        self.assertTrue(call.kwargs['only_not_analyzed'])

    def test_no_eligible_id_never_falls_back_to_other_posts(self):
        with self.mocked_cli() as mocks:
            mocks['distinct_profile_usernames_recent'].return_value = []
            with self.assertRaises(SystemExit) as exc:
                serial.main(['--post-id', '6', '--reanalyze'])
        self.assertEqual(exc.exception.code, 1)
        mocks['process_profile_db'].assert_not_called()

    def test_invalid_or_legacy_arguments_rejected_before_client_creation(self):
        for args in (
            ['--post-id', '6', '--use-temp-download'],
            ['--post-id', '0'], ['--post-id', '-1'], ['--post-id', 'not-an-id'],
        ):
            with self.subTest(args=args), self.mocked_cli() as mocks:
                with self.assertRaises(SystemExit) as exc:
                    serial.main(args)
                self.assertEqual(exc.exception.code, 2)
                mocks['gemini_api_key'].assert_not_called()
                mocks['client'].assert_not_called()
                mocks['process_profile_db'].assert_not_called()

    def test_process_db_uses_id_filter_and_zero_limit_does_not_attempt(self):
        with patch.object(serial, 'fetch_instagram_rows_for_profile', return_value=[SimpleNamespace(id=6)]) as fetch, \
             patch.object(serial, 'analyze_post') as analyze:
            serial.process_profile_db('acm.at.ucsd', client=Mock(), model_chain=['m'], tz=TZ,
                limit=0, only_not_analyzed=False, sleep_s=0, post_id=6)
        fetch.assert_called_once_with('acm.at.ucsd', only_not_analyzed=False, post_id=6)
        analyze.assert_not_called()

    def test_profile_mismatch_or_ineligible_row_does_not_analyze(self):
        with patch.object(serial, 'fetch_instagram_rows_for_profile', return_value=[]) as fetch, \
             patch.object(serial, 'analyze_post') as analyze:
            serial.process_profile_db('another.profile', client=Mock(), model_chain=['m'], tz=TZ,
                limit=1, only_not_analyzed=False, sleep_s=0, post_id=6)
        fetch.assert_called_once_with('another.profile', only_not_analyzed=False, post_id=6)
        analyze.assert_not_called()
