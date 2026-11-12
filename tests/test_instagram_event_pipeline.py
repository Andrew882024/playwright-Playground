"""Offline tests for optional current-run covers; no real pipeline steps execute."""
import ast
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy.dialects import postgresql

from test_instagram_event_extraction import serial, ga, row_six, TZ
from app.event_cover_service import pipeline_stage as stage
from app.event_cover_service import openai_cover
from app.routes import instagram_pipeline as route


ENABLED = {'INSTAGRAM_PIPELINE_GENERATE_COVERS': 'true', 'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS': '1'}


class CoverStageTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.env = self.stack.enter_context(patch.dict(os.environ, ENABLED))
        self.query = self.stack.enter_context(patch.object(stage, 'parents_for_posts', return_value=[
            (1, None, 'source', None), (2, None, 'source', None),
        ]))
        self.settings = self.stack.enter_context(patch.object(stage.Settings, 'from_env', return_value=Mock()))
        self.cover = self.stack.enter_context(patch.object(stage, 'run_cover', return_value={'mode': 'generated'}))

    def test_disabled_by_default_does_not_read_db_or_call_cover(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GENERATE_COVERS': ''}):
            result = stage.run_optional_cover_stage({6})
        self.assertFalse(result['enabled'])
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual([result[k] for k in ('attempted', 'published', 'skipped', 'failed')], [0, 0, 0, 0])
        self.query.assert_not_called()
        self.settings.assert_not_called()
        self.cover.assert_not_called()

    def test_empty_current_run_does_not_scan_backlog(self):
        result = stage.run_optional_cover_stage(set())
        self.assertEqual(result['attempted'], 0)
        self.query.assert_not_called()
        self.cover.assert_not_called()

    def test_default_cap_is_one_and_limits_attempts_not_successes(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS': ''}):
            self.cover.side_effect = RuntimeError('offline-secret')
            result = stage.run_optional_cover_stage({6})
        self.assertEqual(result['max_attempts'], 1)
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['status'], 'partial_failure')
        self.cover.assert_called_once()
        self.assertNotIn('offline-secret', str(result))

    def test_only_current_run_parent_set_is_queried(self):
        stage.run_optional_cover_stage({6, 9})
        self.query.assert_called_once_with({6, 9})

    def test_skip_generated_and_sourceless_before_using_cap(self):
        self.query.return_value = [(1, 'already', 'source', None), (2, None, ' ', ''),
                                   (3, '  ', 'source', None), (4, None, None, 'original-s3')]
        result = stage.run_optional_cover_stage({6})
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(result['published'], 1)
        self.assertEqual(result['skipped'], 3)
        self.assertEqual(self.cover.call_args.args[1], 3)
        self.assertEqual([c['reason'] for c in result['parents'] if c['status'] == 'skipped'],
                         ['already_generated', 'missing_source', 'attempt_cap'])

    def test_parent_original_s3_source_is_sufficient(self):
        self.query.return_value = [(7, None, None, 'original-s3')]
        result = stage.run_optional_cover_stage({6})
        self.assertEqual(result['published'], 1)
        self.cover.assert_called_once_with(stage.SessionLocal, 7, self.settings.return_value,
                                           dry_run=False, allow_generation=True, generation_guard=stage.require_generation_date)

    def test_failures_isolated_per_parent_and_recovery_uses_same_helper(self):
        self.cover.side_effect = [RuntimeError('offline failure'), {'mode': 'resumed'}]
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS': '2'}):
            result = stage.run_optional_cover_stage({6})
        self.assertEqual([result[k] for k in ('attempted', 'published', 'skipped', 'failed')], [2, 1, 0, 1])
        self.assertEqual(self.cover.call_count, 2)

    def test_already_published_race_counts_as_skip(self):
        self.query.return_value = [(1, None, 'source', None)]
        self.cover.return_value = {'mode': 'already-published'}
        result = stage.run_optional_cover_stage({6})
        self.assertEqual([result[k] for k in ('attempted', 'published', 'skipped', 'failed')], [1, 0, 1, 0])

    def test_zero_or_invalid_cap_cannot_trigger_generation(self):
        for cap in ('0', '-1', 'invalid'):
            with self.subTest(cap=cap), patch.dict(os.environ, {'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS': cap}):
                self.cover.reset_mock()
                result = stage.run_optional_cover_stage({6})
                self.assertEqual(result['attempted'], 0)
                self.cover.assert_not_called()
                if cap != '0':
                    self.assertEqual(result['error'], 'invalid_cover_attempt_cap')

    def test_candidate_query_or_settings_failure_does_not_raise(self):
        self.query.side_effect = RuntimeError('offline-secret')
        result = stage.run_optional_cover_stage({6})
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('offline-secret', str(result))
        self.cover.assert_not_called()


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        setting = patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': ''})
        setting.start()
        self.addCleanup(setting.stop)

    def test_query_restricts_parents_with_no_history_fallback(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.execute.return_value.all.return_value = [(1, None, 'source', None)]
        with patch.object(stage, 'SessionLocal', factory):
            self.assertEqual(len(stage.parents_for_posts({6, 9})), 1)
        stmt = session.execute.call_args.args[0]
        compiled = stmt.compile(dialect=postgresql.dialect())
        self.assertIn('instagram_posts.id IN', str(compiled))
        self.assertIn([6, 9], compiled.params.values())
        self.assertIn('instagram_posts.ai_analyzed IS true', str(compiled))
        self.assertIn('ORDER BY instagram_posts.id ASC', str(compiled))
        with patch.object(stage, 'SessionLocal') as session_factory:
            self.assertEqual(stage.parents_for_posts(set()), [])
            session_factory.assert_not_called()

    def test_pipeline_order_collects_only_its_own_commits(self):
        order = []
        def analyze(argv, *, committed_parent_ids, analysis_stats=None):
            self.assertEqual(argv, [])
            order.append('analysis')
            committed_parent_ids.add(6)
        def backfill(argv):
            self.assertEqual(argv, [])
            order.append('original_backfill')
        def covers(ids):
            order.append('covers')
            self.assertEqual(ids, {6})
            return dict(attempted=1, published=0, skipped=0, failed=1)
        with tempfile.TemporaryDirectory() as tmp, patch.object(route, 'PROJECT_ROOT', Path(tmp)), \
             patch.object(route, 'run_instagram_scraper', side_effect=lambda: order.append('scrape')), \
             patch.object(route, 'ai_analyze_main', side_effect=analyze), \
             patch.object(route, 'run_add_our_own_s3_url', side_effect=backfill), \
             patch.object(route, 'run_optional_cover_stage', side_effect=covers):
            result = route.post_instagram_pipeline()
        self.assertEqual(order, ['scrape', 'analysis', 'original_backfill', 'covers'])
        self.assertTrue(result['success'])
        self.assertEqual(result['cover_stage']['failed'], 1)
        self.assertEqual(result['steps_completed'], ['sync_ins', 'ai_analyze', 'add_our_own_s3_url'])

    def test_real_stage_failure_is_reported_separately_from_pipeline_success(self):
        def analyze(argv, *, committed_parent_ids, analysis_stats=None):
            committed_parent_ids.add(6)
        with tempfile.TemporaryDirectory() as tmp, patch.object(route, 'PROJECT_ROOT', Path(tmp)), \
             patch.object(route, 'run_instagram_scraper'), patch.object(route, 'ai_analyze_main', side_effect=analyze), \
             patch.object(route, 'run_add_our_own_s3_url'), patch.dict(os.environ, ENABLED), \
             patch.object(stage, 'parents_for_posts', return_value=[(1, None, 'source', None)]), \
             patch.object(stage.Settings, 'from_env', return_value=Mock()), \
             patch.object(stage, 'run_cover', side_effect=RuntimeError('offline-secret')):
            result = route.post_instagram_pipeline()
            log = Path(result['log_file']).read_text()
        self.assertTrue(result['success'])
        self.assertEqual(result['cover_stage']['failed'], 1)
        self.assertIn('Generated-cover stage:', log)
        self.assertNotIn('offline-secret', log)

    def test_collector_records_only_successful_commits(self):
        rows = [SimpleNamespace(id=i, post_shortcode=str(i)) for i in (6, 7, 8)]
        ai = ga.PostAnalysisAI.model_validate(row_six())
        collected = {99}
        with patch.object(serial, 'fetch_instagram_rows_for_profile', return_value=rows), \
             patch.object(serial, 'build_analyzer_post_dict', side_effect=lambda row: {'shortcode': row.post_shortcode}), \
             patch.object(serial, 'analyze_post', return_value=(ai, 0)), \
             patch.object(serial, 'persist_analysis', side_effect=[True, False, RuntimeError('commit failure')]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            serial.process_profile_db('acm.at.ucsd', client=Mock(), model_chain=['model'], tz=TZ, limit=None,
                                      only_not_analyzed=True, sleep_s=0, committed_parent_ids=collected)
        self.assertEqual(collected, {99, 6})

    def test_main_passes_optional_collector_without_new_cli_flags(self):
        collected = set()
        def process(*args, **kwargs):
            self.assertIs(kwargs['committed_parent_ids'], collected)
            kwargs['committed_parent_ids'].add(6)
            return 0
        with patch.object(serial, 'gemini_api_key', return_value='offline'), \
             patch.object(serial, 'gemini_model_chain', return_value=['model']), \
             patch.object(serial.genai, 'Client'), patch.object(serial, 'process_profile_db', side_effect=process), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            serial.main(['--profile', 'acm.at.ucsd', '--post-id', '6', '--limit', '1'], committed_parent_ids=collected)
        self.assertEqual(collected, {6})

    def test_stage_uses_existing_cover_function_without_duplicate_generation_logic(self):
        from app.event_cover_service.parent_cover import run_parent_cover
        self.assertIs(stage.run_cover, run_parent_cover)
        tree = ast.parse(Path(stage.__file__).read_text())
        self.assertEqual([node.func.id for node in ast.walk(tree)
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'run_cover'], ['run_cover'])
        text = Path(stage.__file__).read_text()
        for forbidden in ('httpx', 'boto3', 'images/edits', 'edit_image(', 'upload_png(', 'argparse'):
            self.assertNotIn(forbidden, text)


class GlobalGeminiBudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.profiles = {
            'alpha': [SimpleNamespace(id=i, post_shortcode=f'alpha{i}') for i in (1, 2)],
            'beta': [SimpleNamespace(id=i, post_shortcode=f'beta{i}') for i in (3, 4)],
        }
        self.stack.enter_context(patch.object(serial, 'gemini_api_key', return_value='offline'))
        self.stack.enter_context(patch.object(serial, 'gemini_model_chain', return_value=['model']))
        self.stack.enter_context(patch.object(serial.genai, 'Client'))
        self.stack.enter_context(patch.object(serial, 'distinct_profile_usernames_recent', return_value=['alpha', 'beta']))
        self.fetch = self.stack.enter_context(patch.object(serial, 'fetch_instagram_rows_for_profile',
            side_effect=lambda profile, **kwargs: self.profiles[profile]))
        self.stack.enter_context(patch.object(serial, 'build_analyzer_post_dict',
            side_effect=lambda row: {'shortcode': row.post_shortcode}))
        ai = ga.PostAnalysisAI.model_validate(row_six())
        self.analyze = self.stack.enter_context(patch.object(serial, 'analyze_post', return_value=(ai, 0)))
        self.persist = self.stack.enter_context(patch.object(serial, 'persist_analysis', return_value=True))

    def test_global_cap_one_stops_across_remaining_profiles_and_collects_commit(self):
        committed = set()
        serial.main(['--sleep', '0'], max_attempts=1, committed_parent_ids=committed)
        self.analyze.assert_called_once()
        self.assertEqual(self.analyze.call_args.args[2]['shortcode'], 'alpha1')
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(committed, {1})

    def test_failed_post_consumes_global_budget(self):
        self.analyze.side_effect = RuntimeError('offline failure')
        committed = set()
        serial.main(['--sleep', '0'], max_attempts=1, committed_parent_ids=committed)
        self.analyze.assert_called_once()
        self.persist.assert_not_called()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(committed, set())

    def test_global_budget_carries_over_between_profiles(self):
        committed = set()
        serial.main(['--sleep', '0'], max_attempts=3, committed_parent_ids=committed)
        self.assertEqual(self.analyze.call_count, 3)
        self.assertEqual([call.args[2]['shortcode'] for call in self.analyze.call_args_list],
                         ['alpha1', 'alpha2', 'beta3'])
        self.assertEqual(committed, {1, 2, 3})

    def test_unlimited_when_programmatic_cap_omitted(self):
        serial.main(['--sleep', '0'])
        self.assertEqual(self.analyze.call_count, 4)
        self.assertEqual(self.fetch.call_count, 2)

    def test_cli_limit_stays_per_profile_and_ignores_pipeline_environment(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': '1'}):
            serial.main(['--limit', '1', '--sleep', '0'])
        self.assertEqual(self.analyze.call_count, 2)
        self.assertEqual([call.args[2]['shortcode'] for call in self.analyze.call_args_list], ['alpha1', 'beta3'])

    def test_cli_limit_and_global_cap_both_respected(self):
        serial.main(['--limit', '1', '--sleep', '0'], max_attempts=1)
        self.analyze.assert_called_once()

    def test_empty_profile_does_not_consume_budget(self):
        self.profiles['alpha'] = []
        serial.main(['--sleep', '0'], max_attempts=1)
        self.analyze.assert_called_once()
        self.assertEqual(self.analyze.call_args.args[2]['shortcode'], 'beta3')

    def test_zero_global_budget_skips_candidates_and_analysis(self):
        serial.main([], max_attempts=0)
        self.fetch.assert_not_called()
        self.analyze.assert_not_called()


class PipelineGlobalBudgetConfigTests(unittest.TestCase):
    def test_unset_or_blank_is_unlimited(self):
        with patch.dict(os.environ):
            os.environ.pop('INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS', None)
            self.assertIsNone(route._gemini_max_attempts())
        for value in ('', '  '):
            with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': value}):
                self.assertIsNone(route._gemini_max_attempts())

    def test_invalid_or_negative_setting_stops_before_scraping(self):
        for value in ('-1', 'not-a-number'):
            with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': value}), \
                 patch.object(route, 'run_instagram_scraper') as scrape, \
                 patch.object(route, 'ai_analyze_main') as analyze:
                with self.assertRaises(route.HTTPException):
                    route.post_instagram_pipeline()
            scrape.assert_not_called()
            analyze.assert_not_called()

    def test_full_scraper_invocation_unchanged_with_capped_analysis(self):
        from app.instagram_scraper import sync_ins
        original_accounts = list(sync_ins.TARGET_URLS)
        self.assertGreater(len(original_accounts), 1)
        with tempfile.TemporaryDirectory() as tmp, patch.object(route, 'PROJECT_ROOT', Path(tmp)), \
             patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': '1'}), \
             patch.object(route, 'run_instagram_scraper') as scrape, \
             patch.object(route, 'ai_analyze_main') as analyze, \
             patch.object(route, 'run_add_our_own_s3_url'), \
             patch.object(route, 'run_optional_cover_stage', return_value={'enabled': False}):
            result = route.post_instagram_pipeline()
        self.assertTrue(result['success'])
        scrape.assert_called_once_with()  # No profile restriction or replacement account list.
        self.assertEqual(sync_ins.TARGET_URLS, original_accounts)
        self.assertEqual(analyze.call_args.args, ([],))
        self.assertEqual(analyze.call_args.kwargs['max_attempts'], 1)
        self.assertIsInstance(analyze.call_args.kwargs['committed_parent_ids'], set)

    def test_uncapped_pipeline_omits_new_programmatic_option(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(route, 'PROJECT_ROOT', Path(tmp)), \
             patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': ''}), \
             patch.object(route, 'run_instagram_scraper'), patch.object(route, 'ai_analyze_main') as analyze, \
             patch.object(route, 'run_add_our_own_s3_url'), \
             patch.object(route, 'run_optional_cover_stage', return_value={'enabled': False}):
            route.post_instagram_pipeline()
        self.assertNotIn('max_attempts', analyze.call_args.kwargs)


class OriginalBackfillArgvTests(unittest.TestCase):
    def setUp(self):
        from app.add_our_own_s3_url import add_our_own_s3_url
        self.backfill = add_our_own_s3_url

    def test_explicit_empty_args_ignore_uvicorn_process_arguments(self):
        with patch.object(sys, 'argv', ['__main__.py', 'app.main:app', '--host', '127.0.0.1', '--port', '8001']), \
             patch.object(self.backfill, 'run_backfill', return_value=0) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.backfill.main([])
        run.assert_called_once_with(profile=None, limit=None, dry_run=False)

    def test_default_and_explicit_none_preserve_standalone_arguments(self):
        for explicit_none in (False, True):
            with self.subTest(explicit_none=explicit_none), \
                 patch.object(sys, 'argv', ['backfill.py', '--profile', 'acm.at.ucsd', '--limit', '2', '--dry-run']), \
                 patch.object(self.backfill, 'run_backfill', return_value=2) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                if explicit_none:
                    self.backfill.main(None)
                else:
                    self.backfill.main()
            run.assert_called_once_with(profile='acm.at.ucsd', limit=2, dry_run=True)

    def test_standalone_unknown_arguments_still_fail_before_backfill(self):
        with patch.object(sys, 'argv', ['backfill.py', '--unknown']), \
             patch.object(self.backfill, 'run_backfill') as run, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                self.backfill.main()
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_pipeline_calls_real_parser_with_empty_args_before_covers(self):
        order = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(route, 'PROJECT_ROOT', Path(tmp)), \
             patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': ''}), \
             patch.object(sys, 'argv', ['__main__.py', 'app.main:app', '--host', '127.0.0.1', '--port', '8001']), \
             patch.object(route, 'run_instagram_scraper'), patch.object(route, 'ai_analyze_main'), \
             patch.object(self.backfill, 'run_backfill', side_effect=lambda **kwargs: order.append('backfill') or 0) as run, \
             patch.object(route, 'run_optional_cover_stage', side_effect=lambda ids: order.append('covers') or {'enabled': False}):
            result = route.post_instagram_pipeline()
        self.assertTrue(result['success'])
        run.assert_called_once_with(profile=None, limit=None, dry_run=False)
        self.assertEqual(order, ['backfill', 'covers'])


class CoverFailureDiagnosticTests(unittest.TestCase):
    def test_cover_error_logged_safely_and_child_failure_still_isolated(self):
        failure = openai_cover.CoverError('Reference image download failed (HTTP 403); no generation performed.',
                                          stage='source_download')
        output = io.StringIO()
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GENERATE_COVERS': 'true',
                                    'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS': '2'}), \
             patch.object(stage, 'parents_for_posts', return_value=[(3, None, 'source', None), (4, None, 'source', None)]), \
             patch.object(stage.Settings, 'from_env', return_value=Mock()), \
             patch.object(stage, 'run_cover', side_effect=[failure, {'mode': 'resumed'}]), \
             contextlib.redirect_stdout(output):
            result = stage.run_optional_cover_stage({289})
        self.assertEqual(result['parents'][0], {'parent_id': 3, 'status': 'failed', 'reason': 'cover_failed'})
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['published'], 1)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertIn('"parent_id": 3', output.getvalue())
        self.assertIn('"stage": "source_download"', output.getvalue())
        self.assertIn(str(failure), output.getvalue())

    def test_unexpected_error_logs_only_class_and_safe_stage(self):
        failure = RuntimeError('https://private.invalid/?key=offline-secret Authorization: Bearer token private-body')
        failure.stage = 'source_download'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stage._log_cover_failure(3, failure)
        self.assertIn('"parent_id": 3', output.getvalue())
        self.assertIn('"stage": "source_download"', output.getvalue())
        self.assertIn('"exception_class": "RuntimeError"', output.getvalue())
        for secret in ('https://', 'offline-secret', 'Authorization', 'Bearer', 'private-body'):
            self.assertNotIn(secret, output.getvalue())
        self.assertNotIn('"error":', output.getvalue())

    def test_unknown_stage_metadata_is_not_logged(self):
        failure = RuntimeError('offline-secret')
        failure.stage = 'https://private.invalid/offline-secret'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stage._log_cover_failure(3, failure)
        self.assertNotIn('"stage":', output.getvalue())
        self.assertNotIn('offline-secret', output.getvalue())

    def test_cover_error_without_stage_keeps_safe_message(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stage._log_cover_failure(3, openai_cover.CoverError('Saved PNG is missing; no automatic regeneration.'))
        self.assertIn('Saved PNG is missing', output.getvalue())
        self.assertNotIn('"stage":', output.getvalue())
