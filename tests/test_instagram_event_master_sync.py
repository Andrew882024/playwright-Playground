"""Offline Event Master integration tests; the real importer is never executed."""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy.dialects import postgresql

from test_instagram_event_extraction import serial  # Import ORM with a mocked engine.
from app.event_master_sync import sync_service as sync
from app.routes import instagram_pipeline as route
import sync_instagram_children_to_event_master as cli


class SyncServiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.backend = Path(tmp) / 'backend repo'
        self.python = self.backend / 'venv/bin/python'
        self.python.parent.mkdir(parents=True)
        self.python.write_text('offline placeholder, never executed')
        self.python.chmod(0o700)
        (self.backend / 'import_instagram_event.py').write_text('# offline placeholder, never executed')
        self.settings = sync.SyncSettings(self.backend, self.python, timeout=12)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.run = self.stack.enter_context(patch.object(sync.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{"status":"succeeded","action":"unchanged","post_id":289,"event_id":15}')))

    def test_disabled_mode_does_not_select_or_invoke_anything(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER': ''}), \
             patch.object(sync, 'children_for_parents') as children, \
             patch.object(sync.SyncSettings, 'from_env') as settings:
            result = sync.run_optional_event_master_sync({289})
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(result['attempted'], 0)
        children.assert_not_called(); settings.assert_not_called(); self.run.assert_not_called()

    def test_current_run_parent_query_includes_multiple_children_without_cover_filters(self):
        import Blueprint_db
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.scalars.return_value.all.return_value = [3, 4, 5, 6]
        with patch.object(Blueprint_db, 'SessionLocal', factory):
            self.assertEqual(sync.children_for_parents({289}), [3, 4, 5, 6])
        statement = session.scalars.call_args.args[0]
        compiled = statement.compile(dialect=postgresql.dialect())
        self.assertIn('instagram_events.instagram_post_id IN', str(compiled))
        self.assertIn([289], compiled.params.values())
        for forbidden in ('generated_image_url', 'is_duplicated', 'is_event', 'main_image_url'):
            self.assertNotIn(forbidden, str(compiled))
        with patch.object(Blueprint_db, 'SessionLocal') as no_query:
            self.assertEqual(sync.children_for_parents(set()), [])
        no_query.assert_not_called()

    def test_enabled_stage_uses_once_per_current_parent_not_children(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER': 'true'}), \
             patch.object(sync, 'children_for_parents') as children, \
             patch.object(sync.SyncSettings, 'from_env', return_value=self.settings):
            result = sync.run_optional_event_master_sync({289})
        children.assert_not_called()
        self.assertEqual(result['succeeded'], 1)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0][-3:], ['--instagram-post-id', '289', '--sync'])

    def test_no_current_commits_never_scans_backlog(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER': 'true'}), \
             patch.object(sync, 'children_for_parents') as children:
            result = sync.run_optional_event_master_sync(set())
        self.assertEqual(result['attempted'], 0)
        children.assert_not_called(); self.run.assert_not_called()

    def test_failure_timeout_and_start_error_do_not_stop_later_children(self):
        for failure in (SimpleNamespace(returncode=1),
                        subprocess.TimeoutExpired(['hidden'], 12, output=b'offline-secret', stderr=b'offline-password'),
                        OSError('offline-password')):
            with self.subTest(failure=type(failure).__name__):
                self.run.reset_mock()
                self.run.side_effect = [failure, SimpleNamespace(returncode=0)]
                result = sync.sync_children([3, 4], self.settings)
                self.assertEqual(result['attempted'], 2)
                self.assertEqual(result['succeeded'], 1)
                self.assertEqual(result['failed'], 1)
                self.assertEqual(result['children'][1]['status'], 'succeeded')
                self.assertEqual(result['status'], 'partial_failure')
                if isinstance(failure, subprocess.TimeoutExpired):
                    self.assertEqual(result['children'][0]['reason'], 'timeout_commit_status_unknown')
                self.assertNotIn('offline-secret', json.dumps(result))
                self.assertNotIn('offline-password', json.dumps(result))

    def test_exact_argument_list_cwd_timeout_and_noninteractive_process(self):
        result = sync.sync_children([3], self.settings)
        self.assertEqual(result['succeeded'], 1)
        call = self.run.call_args
        self.assertEqual(call.args[0], [str(self.python), str(self.backend / 'import_instagram_event.py'),
                                       '--instagram-event-id', '3'])
        self.assertIs(call.kwargs['shell'], False)
        self.assertEqual(call.kwargs['cwd'], str(self.backend))
        self.assertEqual(call.kwargs['timeout'], 12)
        self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(call.kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(call.kwargs['stderr'], subprocess.PIPE)
        self.assertIs(call.kwargs['check'], False)

    def test_known_stderr_messages_map_only_to_safe_reason_codes(self):
        cases = {
            b'Set INSTAGRAM_DB_PASSWORD for noninteractive use.': 'source_password_missing',
            b'Required MySQL DB_* configuration is missing.': 'mysql_configuration_missing',
            b'MySQL DB_HOSTNAME must be a local loopback host.': 'mysql_host_not_local',
            b'Another import for this child is running; try later.': 'import_lock_unavailable',
        }
        for message, reason in cases.items():
            for ending in (b'', b'\n', b'\r\n'):
                with self.subTest(reason=reason, ending=ending):
                    self.run.return_value = SimpleNamespace(returncode=1, stderr=message + ending)
                    result = sync.sync_children([3], self.settings)
                    self.assertEqual(result['children'][0]['reason'], reason)
                    self.assertNotIn(message.decode(), json.dumps(result) + self.output.getvalue())

    def test_unknown_or_secret_stderr_is_never_exposed_by_stage(self):
        safe = b'Set INSTAGRAM_DB_PASSWORD for noninteractive use.'
        for message in (b'offline-password=secret-test-value',
                        safe + b'\noffline-password=secret-test-value',
                        b'offline-password=secret-test-value\n' + safe,
                        b' ' + safe, b'\xff\xfe', b'', None):
            with self.subTest(message_type=type(message).__name__):
                self.run.return_value = SimpleNamespace(returncode=1, stderr=message)
                with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER': 'true'}), \
                     patch.object(sync, 'children_for_parents', return_value=[3]), \
                     patch.object(sync.SyncSettings, 'from_env', return_value=self.settings):
                    result = sync.run_optional_event_master_sync({289})
                self.assertEqual(result['parents'][0]['reason'], 'importer_failed')
                exposed = json.dumps(result) + self.output.getvalue()
                self.assertNotIn('secret-test-value', exposed)
                self.assertNotIn(safe.decode(), exposed)

    def test_environment_does_not_leak_credentials_or_python_paths(self):
        secrets = dict(DB_HOSTNAME='playground-host', DB_PORT='5439', DB_DATABASE='playground',
                       DB_USERNAME='playground', DB_PASSWORD='offline-password', DATABASE_URL='offline-db-url',
                       INSTAGRAM_DB_PASSWORD='playground-source-password',
                       PYTHONPATH='/playground/app', PYTHONHOME='/playground/python', VIRTUAL_ENV='/playground/venv',
                       OPENAI_API_KEY='offline-openai', AWS_SECRET_ACCESS_KEY='offline-aws',
                       PYTHONSTARTUP='/untrusted/startup.py')
        with patch.dict(os.environ, secrets):
            result = sync.sync_children([3], self.settings)
        env = self.run.call_args.kwargs['env']
        for key in secrets:
            self.assertNotIn(key, env)
        self.assertEqual(env['PYTHONNOUSERSITE'], '1')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        for secret in secrets.values():
            self.assertNotIn(secret, str(self.run.call_args.args))
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, self.output.getvalue())

    def test_explicit_ids_deduplicated_without_import_mapping(self):
        result = sync.sync_children([4, 3, 3], self.settings)
        self.assertEqual(result['attempted'], 2)
        self.assertEqual([c.args[0][-1] for c in self.run.call_args_list], ['3', '4'])
        text = Path(sync.__file__).read_text()
        for forbidden in ('map_event(', 'import_marker(', 'GET_LOCK', 'mysql+pymysql', 'INSERT INTO', 'America/Los_Angeles'):
            self.assertNotIn(forbidden, text)

    def test_bad_configuration_is_safe_and_starts_no_importer(self):
        for settings in (sync.SyncSettings(Path('relative'), self.python),
                         sync.SyncSettings(self.backend, Path('relative')),
                         sync.SyncSettings(self.backend, self.python, timeout=float('nan')),
                         sync.SyncSettings(self.backend, self.python, timeout=0),
                         sync.SyncSettings(self.backend, self.backend / 'missing-python')):
            result = sync.sync_children([3], settings)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['attempted'], 0)
            self.run.assert_not_called()

    def test_interpreter_symlink_is_not_resolved_out_of_venv(self):
        self.python.unlink()
        self.python.symlink_to(Path(sys.executable).resolve())
        with patch.dict(os.environ, {'EVENT_MASTER_BACKEND_DIR': str(self.backend),
                                    'EVENT_MASTER_IMPORT_PYTHON': str(self.python),
                                    'EVENT_MASTER_IMPORT_TIMEOUT_SECONDS': '17'}):
            settings = sync.SyncSettings.from_env()
        self.assertEqual(settings.python, self.python)
        self.assertEqual(settings.timeout, 17)
        sync.sync_children([3], settings)
        self.assertEqual(self.run.call_args.args[0][0], str(self.python))

    def test_selection_failure_does_not_fail_pipeline_stage(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER': 'true'}), \
             patch.object(sync, 'sync_posts', side_effect=RuntimeError('offline-password')):
            result = sync.run_optional_event_master_sync({289})
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('offline-password', self.output.getvalue())
        self.run.assert_not_called()


class SyncCliAndPipelineTests(unittest.TestCase):
    def test_sync_only_cli_defaults_to_no_import_and_explicit_ids(self):
        with patch.object(sync, 'sync_children') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['--child-id', '3']), 0)
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {'child_ids': [3], 'status': 'dry-run'})

    def test_explicit_sync_works_without_enabling_automatic_pipeline(self):
        with patch.dict(os.environ, {'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER': 'false'}), \
             patch.object(sync, 'sync_children', return_value=dict(status='completed', attempted=1)) as run, \
             patch.object(route, 'run_instagram_scraper') as scrape, \
             patch.object(route, 'ai_analyze_main') as analyze, \
             patch.object(route, 'run_add_our_own_s3_url') as original, \
             patch.object(route, 'run_optional_cover_stage') as covers, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--child-id', '3', '--sync']), 0)
        run.assert_called_once_with([3])
        for mock in (scrape, analyze, original, covers):
            mock.assert_not_called()
        # The recovery CLI does not import these pipeline stages at all.
        tree = ast.parse(Path(cli.__file__).read_text())
        imports = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any('instagram_scraper' in name or 'ai_analyze' in name or 'event_cover' in name for name in imports))

    def test_cli_invalid_ids_do_not_execute_and_failures_return_nonzero(self):
        with patch.object(sync, 'sync_children') as run, contextlib.redirect_stderr(io.StringIO()):
            for args in ([], ['--child-id', '0'], ['--child-id', '-1'], ['--child-id', 'not-an-id']):
                with self.assertRaises(SystemExit):
                    cli.main(args)
        run.assert_not_called()
        with patch.object(sync, 'sync_children', return_value={'status': 'partial_failure'}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--child-id', '3', '--sync']), 1)

    def test_pipeline_sync_after_failed_or_skipped_covers_keeps_core_success(self):
        for cover_status in ('partial_failure', 'disabled'):
            order = []
            def analyze(argv, *, committed_parent_ids, analysis_stats=None):
                order.append('gemini')
                committed_parent_ids.add(289)
            def event_sync(ids):
                self.assertEqual(ids, {289})
                order.append('event_master')
                return dict(status='partial_failure', attempted=4, succeeded=3, failed=1)
            with tempfile.TemporaryDirectory() as tmp, patch.object(route, 'PROJECT_ROOT', Path(tmp)), \
                 patch.dict(os.environ, {'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS': ''}), \
                 patch.object(route, 'run_instagram_scraper', side_effect=lambda: order.append('scrape')), \
                 patch.object(route, 'ai_analyze_main', side_effect=analyze), \
                 patch.object(route, 'run_add_our_own_s3_url', side_effect=lambda argv: order.append('original')), \
                 patch.object(route, 'run_optional_cover_stage', side_effect=lambda ids: order.append('covers') or {'status': cover_status}), \
                 patch.object(route, 'run_optional_event_master_sync', side_effect=event_sync):
                result = route.post_instagram_pipeline()
            self.assertEqual(order, ['scrape', 'gemini', 'original', 'covers', 'event_master'])
            self.assertTrue(result['success'])
            self.assertEqual(result['event_master_sync']['failed'], 1)
            self.assertEqual(result['cover_stage']['status'], cover_status)
