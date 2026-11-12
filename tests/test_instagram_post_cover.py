"""Parent cover tests: external operations and database sessions are mocked."""
import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from test_instagram_event_cover import PNG, SOURCE, ENV
from test_instagram_post_presentation import parent, children
from app.event_cover_service import openai_cover as core, parent_cover as cover
import add_instagram_post_presentation_columns as migration
import generate_instagram_post_cover as cli


class ParentCoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = core.Settings(Path(self.temp.name), bucket='offline-bucket')
        self.snapshot = cover.snapshot_from_rows(parent(), children())
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, ENV))
        self.load = self.stack.enter_context(patch.object(cover, 'load_snapshot', return_value=self.snapshot))
        self.download = self.stack.enter_context(patch.object(core, 'download_source', return_value=(SOURCE, 'image/jpeg')))
        self.edit = self.stack.enter_context(patch.object(cover, 'edit_image', return_value=PNG))
        self.upload = self.stack.enter_context(patch.object(core, 'upload_png', return_value='https://offline/parent.png'))
        self.publish = self.stack.enter_context(patch.object(cover, 'publish_cover'))

    def run_cover(self, **kwargs):
        return cover.run_parent_cover(Mock(), 6, self.settings, dry_run=False, **kwargs)

    def manifest(self):
        path = next(self.settings.cache_dir.glob('parents/6/*/*/manifest.json'))
        return path, json.loads(path.read_text())

    def test_recovery_generation_guard_blocks_before_download_or_paid_request(self):
        guard=Mock(side_effect=core.CoverError('Expired',reason='skipped_expired'))
        with self.assertRaises(core.CoverError) as failure:
            self.run_cover(allow_generation=True,generation_guard=guard)
        self.assertEqual(failure.exception.reason,'skipped_expired')
        self.download.assert_not_called();self.edit.assert_not_called();self.upload.assert_not_called()

    def test_saved_png_resume_bypasses_generation_date_guard(self):
        self.upload.side_effect=RuntimeError('offline upload failure')
        with self.assertRaises(RuntimeError):self.run_cover(allow_generation=True)
        self.upload.side_effect=None
        guard=Mock(side_effect=core.CoverError('Expired',reason='skipped_expired'))
        result=self.run_cover(allow_generation=False,generation_guard=guard)
        self.assertEqual(result['mode'],'resumed');self.assertEqual(result['api_calls'],0)
        guard.assert_not_called();self.edit.assert_called_once()

    def test_dry_run_does_not_create_artifacts_or_call_external_services(self):
        result = cover.run_parent_cover(Mock(), 6, self.settings)
        self.assertEqual(result['parent_id'], 6)
        self.assertEqual(result['event_title'], 'ACM — 2026-10-20')
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(list(self.settings.cache_dir.iterdir()), [])
        for mock in (self.download, self.edit, self.upload, self.publish):
            mock.assert_not_called()

    def test_one_parent_one_call_all_activities_legacy_child_separate(self):
        legacy = self.settings.cache_dir / '7' / 'legacy.png'
        legacy.parent.mkdir()
        legacy.write_bytes(b'unchanged child 7')
        result = self.run_cover(allow_generation=True)
        self.assertEqual(result['api_calls'], 1)
        self.edit.assert_called_once()
        context = self.edit.call_args.args[2]
        self.assertIn('Coffee Chats', context)
        self.assertIn('Tech Talk', context)
        self.assertEqual(legacy.read_bytes(), b'unchanged child 7')
        self.assertTrue(self.upload.call_args.args[1].startswith('instagram_post_covers/6/'+cover.VERSION+'/'))
        path, manifest = self.manifest()
        self.assertEqual(manifest['parent_id'], 6)
        self.assertNotIn('child_id', manifest)
        self.assertEqual(manifest['state'], 'published')
        self.assertEqual(cover.run_parent_cover(Mock(), 6, self.settings, dry_run=False)['api_calls'], 0)
        self.assertEqual(self.edit.call_count, 1)

    def test_child_cover_is_never_promoted(self):
        snapshot = cover.snapshot_from_rows(parent(), children())
        self.assertIsNone(snapshot.generated_url)
        self.assertNotIn('legacy-child-cover', snapshot.context)
        self.assertNotEqual(core.VERSION, cover.VERSION)
        with self.assertRaises(core.CoverError):
            cover.snapshot_from_rows(parent(), [])

    def test_parent_lock_prevents_concurrent_generation(self):
        with core.child_lock(cover.cache_directory(self.settings, 6)):
            with self.assertRaises(core.CoverError):
                self.run_cover(allow_generation=True)
        self.edit.assert_not_called()
        self.download.assert_not_called()

    def test_saved_png_recovers_after_manifest_write_failure(self):
        save = core.save_manifest
        failed = False
        def fail_once(path, manifest):
            nonlocal failed
            if manifest['state'] == 'generated' and not failed:
                failed = True
                raise OSError('offline manifest failure')
            return save(path, manifest)
        with patch.object(core, 'save_manifest', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.run_cover(allow_generation=True)
        self.assertEqual(self.run_cover(allow_generation=False)['api_calls'], 0)
        self.assertEqual(self.edit.call_count, 1)

    def test_concurrent_parent_cover_rejects_publication(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        p = parent(generated_image_url='other-cover', generated_image_input_hash='other-hash')
        session.execute.return_value.scalar_one_or_none.return_value = p
        session.scalars.return_value.all.return_value = children()
        with self.assertRaisesRegex(core.CoverError, 'concurrently'):
            REAL_PUBLISH(factory, self.snapshot, 'https://offline/new.png', 'new-hash')
        self.assertEqual(p.generated_image_url, 'other-cover')

    def test_upload_failure_resumes_without_new_paid_call(self):
        self.upload.side_effect = [RuntimeError('upload failed'), 'https://offline/parent.png']
        with self.assertRaises(RuntimeError):
            self.run_cover(allow_generation=True)
        self.assertEqual(self.run_cover(allow_generation=False)['api_calls'], 0)
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.download.call_count, 1)
        self.assertEqual(self.upload.call_args_list[0].args[1], self.upload.call_args_list[1].args[1])

    def test_db_failure_resumes_without_upload_or_generation(self):
        self.publish.side_effect = [RuntimeError('publication failed'), None]
        with self.assertRaises(RuntimeError):
            self.run_cover(allow_generation=True)
        self.run_cover(allow_generation=False)
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.upload.call_count, 1)
        self.assertEqual(self.publish.call_count, 2)

    def test_uncertain_paid_request_never_retries(self):
        self.edit.side_effect = core.CoverError('Uncertain paid request')
        with self.assertRaises(core.CoverError):
            self.run_cover(allow_generation=True)
        self.assertEqual(self.manifest()[1]['state'], 'request_started')
        with self.assertRaisesRegex(core.CoverError, 'uncertain'):
            self.run_cover(allow_generation=True)
        self.assertEqual(self.edit.call_count, 1)
        self.upload.assert_not_called()

    def test_resume_only_cannot_authorize_generation(self):
        with self.assertRaisesRegex(core.CoverError, 'resume-only'):
            self.run_cover(allow_generation=False)
        self.edit.assert_not_called()

    def test_parent_fingerprint_changes_for_second_activity(self):
        original = cover.input_hash(self.snapshot, SOURCE, self.settings)
        rows = children()
        rows[1].event_description = 'Changed second activity'
        changed = cover.snapshot_from_rows(parent(), rows)
        self.assertNotEqual(original, cover.input_hash(changed, SOURCE, self.settings))
        self.assertFalse(cover.compatible({'parent_id':6, 'child_id':7}, self.snapshot, self.settings))

    def test_parent_publication_changes_only_parent_cover_fields(self):
        from app.event_cover_service.parent_cover import publish_cover
        # setUp patched publication, so use original function saved below.
        p, rows = parent(), children()
        before = [vars(row).copy() for row in rows]
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.execute.return_value.scalar_one_or_none.return_value = p
        session.scalars.return_value.all.return_value = rows
        REAL_PUBLISH(factory, self.snapshot, 'https://offline/cover.png', 'hash')
        self.assertEqual(p.generated_image_url, 'https://offline/cover.png')
        self.assertEqual(p.generated_image_input_hash, 'hash')
        self.assertEqual([vars(row) for row in rows], before)
        session.add.assert_not_called()

    def test_changed_child_blocks_parent_publication(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.execute.return_value.scalar_one_or_none.return_value = parent()
        rows = children()
        rows[1].location = 'Changed location'
        session.scalars.return_value.all.return_value = rows
        with self.assertRaisesRegex(core.CoverError, 'changed'):
            REAL_PUBLISH(factory, self.snapshot, 'https://offline/cover.png', 'hash')

    def test_http_helper_keeps_timeout_and_one_image(self):
        # Exercise the real wrapper with mocked HTTP; no OpenAI request occurs.
        with patch.object(core.httpx, 'Client') as client:
            response = client.return_value.__enter__.return_value.post.return_value
            response.is_success = True
            response.json.return_value = {'data':[{'b64_json':base64.b64encode(PNG).decode()}]}
            REAL_EDIT(SOURCE, 'image/jpeg', self.snapshot.context, self.settings)
            client.assert_called_once_with(timeout=180, follow_redirects=False)
            fields = client.return_value.__enter__.return_value.post.call_args.kwargs['data']
            self.assertEqual(fields['n'], '1')
            self.assertIn('ALL its activities', fields['prompt'])
            self.assertIn('Coffee Chats', fields['prompt'])
            self.assertIn('Tech Talk', fields['prompt'])

    def test_idempotent_migration_has_only_parent_additions(self):
        import Blueprint_db
        engine = MagicMock()
        with patch.object(Blueprint_db, 'engine', engine), contextlib.redirect_stdout(io.StringIO()):
            migration.main()
        sql = str(engine.begin.return_value.__enter__.return_value.execute.call_args.args[0])
        self.assertIn('ALTER TABLE instagram_posts', sql)
        self.assertEqual(sql.count('ADD COLUMN IF NOT EXISTS'), 3)
        self.assertNotIn('instagram_events', sql)
        self.assertNotIn('UPDATE', sql)

    def test_cli_requires_parent_and_defaults_to_dry_run(self):
        with patch('app.event_cover_service.parent_cover.run_parent_cover', return_value={'mode':'dry-run'}) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--parent-id','6']), 0)
        self.assertTrue(run.call_args.kwargs['dry_run'])
        self.assertFalse(run.call_args.kwargs['allow_generation'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['--parent-id','0'])


REAL_PUBLISH = cover.publish_cover
REAL_EDIT = cover.edit_image
