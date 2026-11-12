"""Offline cover tests: no real HTTP, boto3 or database sessions are used."""
import base64
import contextlib
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from test_instagram_event_extraction import serial  # Imports ORM with an offline engine.
from app.event_cover_service import openai_cover as cover
import generate_instagram_event_cover as cli
import add_instagram_event_cover_columns as migration


def png_fixture():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (cover.PNG_SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', 1536, 864, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\0' * (1536 * 3)) * 864)) + chunk(b'IEND', b''))


PNG = png_fixture()
SOURCE = b'offline-source-poster'
ENV = {'OPENAI_API_KEY': 'offline-placeholder', 'AWS_ACCESS_KEY_ID': 'offline-access',
       'AWS_SECRET_ACCESS_KEY': 'offline-secret'}


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = cover.Settings(Path(self.tmp.name) / 'covers', bucket='offline-bucket')
        self.snapshot = cover.Snapshot(1, 6, 'Coffee Chats', 'https://cdn.cdninstagram.com/original.jpg',
                                       'https://offline-bucket.s3.us-east-1.amazonaws.com/original.jpg')
        self.env = patch.dict(os.environ, ENV)
        self.env.start(); self.addCleanup(self.env.stop)
        self.patches = contextlib.ExitStack(); self.addCleanup(self.patches.close)
        self.load = self.patches.enter_context(patch.object(cover, 'load_snapshot', return_value=self.snapshot))
        self.download = self.patches.enter_context(patch.object(cover, 'download_source', return_value=(SOURCE, 'image/jpeg')))
        self.edit = self.patches.enter_context(patch.object(cover, 'edit_image', return_value=PNG))
        self.upload = self.patches.enter_context(patch.object(cover, 'upload_png', return_value='https://offline-bucket.s3.us-east-1.amazonaws.com/generated.png'))
        self.publish = self.patches.enter_context(patch.object(cover, 'publish_cover'))

    def run_generation(self, **kwargs):
        return cover.run_cover(Mock(), 1, self.settings, dry_run=False, allow_generation=True, **kwargs)

    def manifest(self):
        path = next(self.settings.cache_dir.glob('1/*/*/manifest.json'))
        return path, json.loads(path.read_text())

    def test_exact_frontend_prompt_and_parameters(self):
        # SHA of the exact TS template, replacing ${JSON.stringify(eventTitle)}
        # with {event_title_json}; no dependency on the external frontend checkout.
        self.assertEqual(cover.digest(cover.PROMPT_TEMPLATE.encode()),
                         '8c80b8af85926f287d48ef50b48017e6b204a2552612b55ddb98868152d85d0b')
        title = 'Coffee "聊天"\nSession'
        fields = cover.request_fields(title, 'gpt-image-2')
        self.assertIn(json.dumps(title, ensure_ascii=False), fields['prompt'])
        self.assertEqual({k: v for k, v in fields.items() if k != 'prompt'},
                         dict(model='gpt-image-2', n='1', size='1536x864', quality='medium', output_format='png'))
        # Use the real HTTP helper with a mocked client, overriding setUp's edit mock.
        with patch.object(cover.httpx, 'Client') as client:
            response = client.return_value.__enter__.return_value.post.return_value
            response.is_success = True
            response.json.return_value = {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}
            self.assertEqual(REAL_EDIT(SOURCE, 'image/jpeg', title, self.settings), PNG)
            client.assert_called_once_with(timeout=180, follow_redirects=False)
            call = client.return_value.__enter__.return_value.post.call_args
            self.assertEqual(call.args, ('https://api.openai.com/v1/images/edits',))
            self.assertEqual(call.kwargs['files'], {'image[]': ('poster', SOURCE, 'image/jpeg')})
            self.assertEqual(call.kwargs['data'], fields)
            self.assertEqual(call.kwargs['headers'], {'Authorization': 'Bearer offline-placeholder'})

    def test_dry_run_reads_only_and_does_not_create_cache(self):
        result = cover.run_cover(Mock(), 1, self.settings)
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(result['source'], 'parent.own_s3_url_for_main_image')
        self.assertFalse(self.settings.cache_dir.exists())
        for external in (self.download, self.edit, self.upload, self.publish):
            external.assert_not_called()

    def test_generation_saves_then_uploads_then_publishes(self):
        order = []
        def upload(png, key, settings):
            path, manifest = self.manifest()
            self.assertEqual((path.parent / 'generated.png').read_bytes(), PNG)
            self.assertEqual(manifest['state'], 'generated')
            self.assertIn('/1/generated-cover-v2-core-title/', key)
            order.append('upload')
            return 'https://offline/generated.png'
        self.upload.side_effect = upload
        self.publish.side_effect = lambda *args: order.append('publish')
        result = self.run_generation()
        self.assertEqual(result['api_calls'], 1)
        self.assertEqual(order, ['upload', 'publish'])
        self.download.assert_called_once_with(self.snapshot.original_s3_url, self.settings)
        self.edit.assert_called_once_with(SOURCE, 'image/jpeg', 'Coffee Chats', self.settings)
        path, manifest = self.manifest()
        self.assertEqual(manifest['state'], 'published')
        self.assertEqual((path.parent / 'source').read_bytes(), SOURCE)
        self.assertNotIn('offline-secret', path.read_text())
        self.assertNotIn(self.snapshot.source_url, path.read_text())

    def test_s3_failure_resumes_without_download_or_second_generation(self):
        self.upload.side_effect = [RuntimeError('offline upload failure'), 'https://offline/generated.png']
        with self.assertRaises(RuntimeError):
            self.run_generation()
        result = cover.run_cover(Mock(), 1, self.settings, dry_run=False, allow_generation=False)
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.download.call_count, 1)
        self.assertEqual(self.upload.call_count, 2)
        self.assertEqual(self.upload.call_args_list[0].args[1], self.upload.call_args_list[1].args[1])
        self.publish.assert_called_once()

    def test_db_failure_resumes_without_upload_or_generation(self):
        self.publish.side_effect = [RuntimeError('offline DB failure'), None]
        with self.assertRaises(RuntimeError):
            self.run_generation()
        self.assertEqual(self.manifest()[1]['state'], 'uploaded')
        with patch.dict(os.environ, {'OPENAI_API_KEY': '', 'AWS_ACCESS_KEY_ID': '', 'AWS_SECRET_ACCESS_KEY': ''}):
            self.run_generation()
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.upload.call_count, 1)
        self.assertEqual(self.publish.call_count, 2)

    def test_generated_manifest_write_failure_recovers_png(self):
        original = cover.save_manifest
        failed = False
        def save(path, manifest):
            nonlocal failed
            if manifest['state'] == 'generated' and not failed:
                failed = True
                raise OSError('offline disk fault')
            return original(path, manifest)
        with patch.object(cover, 'save_manifest', side_effect=save):
            with self.assertRaises(OSError):
                self.run_generation()
            self.run_generation()
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.manifest()[1]['state'], 'published')

    def test_uploaded_manifest_write_failure_reuses_same_object_key(self):
        original = cover.save_manifest
        def save(path, manifest):
            if manifest['state'] == 'uploaded':
                raise OSError('offline disk fault')
            return original(path, manifest)
        with patch.object(cover, 'save_manifest', side_effect=save), self.assertRaises(OSError):
            self.run_generation()
        self.run_generation()
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.upload.call_count, 2)
        self.assertEqual(self.upload.call_args_list[0].args[1], self.upload.call_args_list[1].args[1])

    def test_crash_after_png_temp_write_is_recoverable(self):
        original = cover.atomic_write
        def write(path, data):
            if path.name == 'generated.png':
                original(path.with_name('generated.png.tmp'), data)
                raise OSError('simulated crash before rename')
            return original(path, data)
        with patch.object(cover, 'atomic_write', side_effect=write), self.assertRaises(OSError):
            self.run_generation()
        self.run_generation()
        self.assertEqual(self.edit.call_count, 1)
        self.assertTrue((self.manifest()[0].parent / 'generated.png').exists())

    def test_timeout_or_malformed_response_never_retries_even_after_restart(self):
        self.edit.side_effect = cover.CoverError('uncertain generation')
        with self.assertRaises(cover.CoverError):
            self.run_generation()
        self.assertEqual(self.manifest()[1]['state'], 'request_started')
        for title in ('Coffee Chats', 'Changed title'):
            self.load.return_value = replace(self.snapshot, title=title)
            with self.assertRaisesRegex(cover.CoverError, 'uncertain'):
                self.run_generation()
        self.assertEqual(self.edit.call_count, 1)
        self.upload.assert_not_called()
        self.publish.assert_not_called()

    def test_single_child_versions_and_changed_title_use_distinct_keys(self):
        self.run_generation()
        first = self.upload.call_args.args[1]
        self.load.return_value = replace(self.snapshot, title='Tech Talk')
        self.run_generation()
        second = self.upload.call_args.args[1]
        self.assertNotEqual(first, second)
        self.load.return_value = replace(self.snapshot, child_id=2)
        cover.run_cover(Mock(), 2, self.settings, dry_run=False, allow_generation=True)
        self.assertIn('instagram_event_covers/2/', self.upload.call_args.args[1])
        self.assertNotEqual(cover.input_hash(self.snapshot, SOURCE, self.settings),
                            cover.input_hash(replace(self.snapshot, child_id=2), SOURCE, self.settings))

    def test_no_second_generation_for_published_asset(self):
        self.run_generation()
        result = self.run_generation()
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.upload.call_count, 1)

    def test_resume_only_never_creates_paid_request(self):
        with self.assertRaisesRegex(cover.CoverError, 'resume-only'):
            cover.run_cover(Mock(), 1, self.settings, dry_run=False, allow_generation=False)
        self.edit.assert_not_called()
        self.download.assert_not_called()

    def test_missing_key_prevents_request_and_source_download(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), self.assertRaises(cover.CoverError):
            self.run_generation()
        self.edit.assert_not_called()
        self.download.assert_not_called()

    def test_lock_prevents_duplicate_request(self):
        with cover.child_lock(self.settings.cache_dir / '1'):
            with self.assertRaisesRegex(cover.CoverError, 'Another process'):
                self.run_generation()
        self.edit.assert_not_called()

    def test_corrupt_artifacts_fail_closed(self):
        self.run_generation()
        self.manifest()[0].write_text('{broken')
        with self.assertRaisesRegex(cover.CoverError, 'Unreadable'):
            self.run_generation()
        self.assertEqual(self.edit.call_count, 1)


# Save real helpers independently of per-test patches.
REAL_EDIT = cover.edit_image
REAL_LOAD = cover.load_snapshot
REAL_PUBLISH = cover.publish_cover


class CoverAdapterTests(unittest.TestCase):
    def test_request_failure_and_invalid_payload_do_not_retry_or_leak_secrets(self):
        for status, body in [(400, {}), (503, {}), (200, {'data': []}),
                             (200, {'data': [{'b64_json': 'invalid'}]})]:
            with patch.dict(os.environ, ENV), patch.object(cover.httpx, 'Client') as client:
                response = client.return_value.__enter__.return_value.post.return_value
                response.is_success = status == 200
                response.status_code = status
                response.json.return_value = body
                with self.assertRaises(cover.CoverError) as error:
                    REAL_EDIT(SOURCE, 'image/jpeg', 'Title', cover.Settings(Path('/tmp/offline')))
                self.assertNotIn('offline-placeholder', str(error.exception))
                self.assertEqual(client.return_value.__enter__.return_value.post.call_count, 1)
        with patch.dict(os.environ, ENV), patch.object(cover.httpx, 'Client') as client:
            client.return_value.__enter__.return_value.post.side_effect = RuntimeError('offline-secret')
            with self.assertRaises(cover.CoverError) as error:
                REAL_EDIT(SOURCE, 'image/jpeg', 'Title', cover.Settings(Path('/tmp/offline')))
            self.assertNotIn('offline-secret', str(error.exception))

    def test_download_limits_and_allowlist_without_network(self):
        for url in ('http://site.cdninstagram.com/a', 'https://example.com/a',
                    'https://user:pass@site.cdninstagram.com/a', 'https://site.cdninstagram.com:443/a'):
            with patch.object(cover.httpx, 'Client') as client, self.assertRaises(cover.CoverError):
                cover.download_source(url)
            client.assert_not_called()
        with patch.object(cover.httpx, 'Client') as client:
            response = client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
            response.status_code = 200
            response.headers = {'content-type': 'image/jpeg'}
            response.iter_bytes.return_value = [b'a' * (cover.MAX_SOURCE_BYTES + 1)]
            with self.assertRaisesRegex(cover.CoverError, '8 MB'):
                cover.download_source('https://site.cdninstagram.com/source.jpg')

    def test_upload_generated_bytes_uses_separate_key_and_image_png(self):
        with patch.dict(os.environ, ENV), patch('boto3.client') as factory:
            url = cover.upload_png(PNG, 'instagram_event_covers/1/v/hash/request.png',
                                   cover.Settings(Path('/tmp/offline'), bucket='offline'))
        call = factory.return_value.put_object.call_args
        self.assertEqual(call.kwargs['ContentType'], 'image/png')
        self.assertEqual(call.kwargs['Body'], PNG)
        self.assertIn('/instagram_event_covers/1/', url)

    def test_load_selects_exact_child_and_requires_successful_parent(self):
        parent = SimpleNamespace(id=6, ai_analyzed=True, main_image_url='https://source/original', own_s3_url_for_main_image='https://s3/original')
        child = SimpleNamespace(id=1, event_title='Coffee', generated_image_url=None, generated_image_input_hash=None)
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.execute.return_value.one_or_none.return_value = (child, parent)
        snapshot = REAL_LOAD(factory, 1)
        self.assertEqual(snapshot.source_url, 'https://s3/original')
        stmt = session.execute.call_args.args[0]
        self.assertIn('instagram_events.id =', str(stmt))
        parent.ai_analyzed = False
        with self.assertRaises(cover.CoverError):
            REAL_LOAD(factory, 1)

    def test_publish_checks_fresh_inputs_and_locks_parent_then_child(self):
        snapshot = cover.Snapshot(1, 6, 'Coffee', 'main', 'own')
        for changed in (False, True):
            factory = MagicMock()
            session = factory.return_value.__enter__.return_value
            parent = SimpleNamespace(id=6, ai_analyzed=True, main_image_url='main', own_s3_url_for_main_image='own')
            child = SimpleNamespace(id=1, instagram_post_id=6, event_title='Changed' if changed else 'Coffee', generated_image_url=None)
            session.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda: parent), SimpleNamespace(scalar_one_or_none=lambda: child)]
            if changed:
                with self.assertRaises(cover.CoverError):
                    REAL_PUBLISH(factory, snapshot, 'generated', 'hash')
                self.assertIsNone(child.generated_image_url)
            else:
                REAL_PUBLISH(factory, snapshot, 'generated', 'hash')
                self.assertEqual(child.generated_image_url, 'generated')
                self.assertEqual(child.generated_image_input_hash, 'hash')
                statements = [c.args[0] for c in session.execute.call_args_list]
                self.assertIsNotNone(statements[0]._for_update_arg)
                self.assertIsNotNone(statements[1]._for_update_arg)
                session.begin.assert_called_once()

    def test_cli_default_inspection_generate_and_resume_modes(self):
        for flags, dry, paid in [([], True, False), (['--dry-run'], True, False),
                                  (['--generate'], False, True), (['--resume-only'], False, False)]:
            with patch.object(cover, 'run_cover', return_value={'api_calls': 0}) as run, \
                 patch.object(cover.Settings, 'from_env', return_value=Mock()), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--child-id', '1', *flags]), 0)
            self.assertEqual(run.call_args.args[1], 1)
            self.assertEqual(run.call_args.kwargs, dict(dry_run=dry, allow_generation=paid))

    def test_cli_rejects_invalid_ids_and_hides_external_errors(self):
        with patch.object(cover, 'run_cover') as run, contextlib.redirect_stderr(io.StringIO()):
            for flags in (['--child-id', '0'], ['--child-id', '1', '--generate', '--dry-run'], []):
                with self.assertRaises(SystemExit):
                    cli.main(flags)
            run.assert_not_called()
        output = io.StringIO()
        with patch.object(cover, 'run_cover', side_effect=RuntimeError('offline-secret')), contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(['--child-id', '1', '--generate']), 1)
        self.assertNotIn('offline-secret', output.getvalue())

    def test_explicit_migration_only_adds_nullable_columns(self):
        import Blueprint_db
        engine = MagicMock()
        with patch.object(Blueprint_db, 'engine', engine), contextlib.redirect_stdout(io.StringIO()):
            migration.main()
            migration.main()
        calls = engine.begin.return_value.__enter__.return_value.execute.call_args_list
        self.assertEqual(len(calls), 2)
        ddl = str(calls[0].args[0])
        self.assertEqual(ddl.count('ADD COLUMN IF NOT EXISTS'), 2)
        self.assertIn('generated_image_url TEXT', ddl)
        self.assertIn('generated_image_input_hash VARCHAR(64)', ddl)
        self.assertNotIn('NOT NULL', ddl)
        self.assertNotIn('instagram_posts', ddl)


class SourceDownloadDiagnosticTests(unittest.TestCase):
    SOURCE_URL = 'https://offline-bucket.s3.us-east-1.amazonaws.com/poster?X-Amz-Signature=offline-secret'

    def test_http_failure_exposes_status_and_stage_only(self):
        for status in (302, 403, 404, 500):
            with self.subTest(status=status), patch.object(cover.httpx, 'Client') as client:
                response = client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
                response.status_code = status
                response.text = 'private-response-body'
                with self.assertRaises(cover.CoverError) as caught:
                    cover.download_source(self.SOURCE_URL)
                self.assertEqual(caught.exception.stage, 'source_download')
                self.assertIn(f'HTTP {status}', str(caught.exception))
                self.assertNotIn(self.SOURCE_URL, str(caught.exception))
                self.assertNotIn('private-response-body', str(caught.exception))
                response.iter_bytes.assert_not_called()

    def test_timeout_and_network_failures_do_not_echo_external_exception(self):
        for error, message in (
            (cover.httpx.ReadTimeout('offline-secret Authorization: Bearer secret'), 'timed out'),
            (cover.httpx.ConnectTimeout(self.SOURCE_URL), 'timed out'),
            (cover.httpx.ConnectError(self.SOURCE_URL), 'connection/network failure'),
            (cover.httpx.ReadError('private-response-body'), 'connection/network failure'),
        ):
            with self.subTest(error=type(error).__name__), patch.object(cover.httpx, 'Client') as client:
                client.return_value.__enter__.return_value.stream.side_effect = error
                with self.assertRaises(cover.CoverError) as caught:
                    cover.download_source(self.SOURCE_URL)
                self.assertEqual(caught.exception.stage, 'source_download')
                self.assertIn(message, str(caught.exception))
                for secret in ('offline-secret', 'Authorization', 'Bearer', self.SOURCE_URL, 'private-response-body'):
                    self.assertNotIn(secret, str(caught.exception))
                client.return_value.__enter__.return_value.stream.assert_called_once()

    def test_body_read_timeout_is_also_source_download(self):
        with patch.object(cover.httpx, 'Client') as client:
            response = client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
            response.status_code = 200
            response.headers = {'content-type': 'image/jpeg'}
            response.iter_bytes.side_effect = cover.httpx.ReadTimeout(self.SOURCE_URL)
            with self.assertRaises(cover.CoverError) as caught:
                cover.download_source(self.SOURCE_URL)
        self.assertEqual(caught.exception.stage, 'source_download')
        self.assertIn('timed out', str(caught.exception))

    def test_existing_validation_and_unexpected_failures_have_stage(self):
        with self.assertRaises(cover.CoverError) as caught:
            cover.download_source('http://not-allowed.invalid/private')
        self.assertEqual(caught.exception.stage, 'source_download')
        with patch.object(cover.httpx, 'Client', side_effect=RuntimeError('offline-secret')):
            with self.assertRaises(RuntimeError) as caught:
                cover.download_source(self.SOURCE_URL)
        self.assertNotIsInstance(caught.exception, cover.CoverError)
        self.assertEqual(caught.exception.stage, 'source_download')


class PrivateS3SourceTests(unittest.TestCase):
    def setUp(self):
        self.settings = cover.Settings(Path('/tmp/offline-unused'), bucket='offline-bucket', region='us-west-2')
        self.url = 'https://offline-bucket.s3.us-west-2.amazonaws.com/instagram_main_images/cover.jpg'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, ENV))
        self.aws = self.stack.enter_context(patch('boto3.client'))
        self.http = self.stack.enter_context(patch.object(cover.httpx, 'Client'))

    def response_body(self, data=b'original-image', mime='image/jpeg'):
        body = Mock(wraps=io.BytesIO(data))
        self.aws.return_value.get_object.return_value = {'Body': body, 'ContentType': mime}
        return body

    def external_response(self, data=b'cdn-image', mime='image/png', status=200):
        response = self.http.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
        response.status_code = status
        response.headers = {'content-type': mime}
        response.iter_bytes.return_value = [data]
        return response

    def test_configured_bucket_uses_authenticated_get_and_decodes_key_once(self):
        body = self.response_body()
        url = ('https://offline-bucket.s3.us-west-2.amazonaws.com/'
               'instagram_main_images/acm/Poster%20%2B%E4%B8%AD%2Fslide%252F1+copy.jpg'
               '?X-Amz-Signature=offline-secret')
        result = cover.download_source(url, self.settings)
        self.assertEqual(result, (b'original-image', 'image/jpeg'))
        self.aws.assert_called_once_with('s3', region_name='us-west-2',
                                         config=unittest.mock.ANY,
                                        aws_access_key_id='offline-access', aws_secret_access_key='offline-secret')
        self.aws.return_value.get_object.assert_called_once_with(
            Bucket='offline-bucket', Key='instagram_main_images/acm/Poster +中/slide%2F1+copy.jpg')
        self.http.assert_not_called()
        body.close.assert_called_once_with()
        self.assertTrue(all(call.args[0] <= 64 * 1024 for call in body.read.call_args_list))

    def test_other_buckets_regions_and_endpoints_never_receive_aws_credentials(self):
        for url in ('https://other-bucket.s3.us-west-2.amazonaws.com/cover.jpg',
                    'https://offline-bucket.s3.us-east-1.amazonaws.com/cover.jpg',
                    'https://offline-bucket.s3.amazonaws.com/cover.jpg'):
            with self.subTest(url=url):
                self.aws.reset_mock(); self.http.reset_mock()
                self.external_response()
                self.assertEqual(cover.download_source(url, self.settings), (b'cdn-image', 'image/png'))
                self.aws.assert_not_called()
                self.http.assert_called_once_with(timeout=15, follow_redirects=False)
                stream = self.http.return_value.__enter__.return_value.stream
                stream.assert_called_once_with('GET', url)

    def test_external_instagram_and_cloudfront_still_use_unchanged_http(self):
        for url in ('https://site.cdninstagram.com/poster.jpg', 'https://distribution.cloudfront.net/poster.jpg'):
            with self.subTest(url=url):
                self.http.reset_mock()
                self.external_response()
                cover.download_source(url, self.settings)
                self.aws.assert_not_called()
                self.http.assert_called_once_with(timeout=15, follow_redirects=False)

    def test_security_checks_run_before_authenticated_download(self):
        for url in ('http://offline-bucket.s3.us-west-2.amazonaws.com/cover.jpg',
                    'https://user:password@offline-bucket.s3.us-west-2.amazonaws.com/cover.jpg',
                    'https://offline-bucket.s3.us-west-2.amazonaws.com:443/cover.jpg',
                    'https://offline-bucket.s3.us-west-2.amazonaws.com.attacker.invalid/cover.jpg'):
            with self.subTest(url=url), self.assertRaises(cover.CoverError) as caught:
                cover.download_source(url, self.settings)
            self.assertEqual(caught.exception.stage, 'source_download')
            self.aws.assert_not_called()
            self.http.assert_not_called()

    def test_invalid_encoded_keys_rejected_without_aws_call(self):
        for path in ('/', '/%ZZ', '/%FF', '/%00private', '/%0Aprivate'):
            url = 'https://offline-bucket.s3.us-west-2.amazonaws.com' + path
            with self.subTest(path=path), self.assertRaises(cover.CoverError) as caught:
                cover.download_source(url, self.settings)
            self.assertEqual(caught.exception.stage, 'source_download')
            self.assertNotIn(path, str(caught.exception))
            self.aws.assert_not_called()

    def test_supported_s3_mimes_and_body_closure_on_success(self):
        for mime in ('image/png', 'image/jpeg', 'image/webp'):
            body = self.response_body(mime=mime + '; charset=binary')
            self.assertEqual(cover.download_source(self.url, self.settings), (b'original-image', mime))
            body.close.assert_called_once_with()

    def test_oversize_s3_body_rejected_and_closed(self):
        body = self.response_body(b'x' * (cover.MAX_SOURCE_BYTES + 1))
        with self.assertRaisesRegex(cover.CoverError, '8 MB') as caught:
            cover.download_source(self.url, self.settings)
        self.assertEqual(caught.exception.stage, 'source_download')
        body.close.assert_called_once_with()
        self.http.assert_not_called()

    def test_exact_size_limit_allowed_and_empty_body_rejected(self):
        body = self.response_body(b'x' * cover.MAX_SOURCE_BYTES)
        data, mime = cover.download_source(self.url, self.settings)
        self.assertEqual(len(data), cover.MAX_SOURCE_BYTES)
        body.close.assert_called_once_with()
        body = self.response_body(b'')
        with self.assertRaisesRegex(cover.CoverError, 'empty'):
            cover.download_source(self.url, self.settings)
        body.close.assert_called_once_with()

    def test_unsupported_mime_rejected_before_read_and_body_closed(self):
        body = self.response_body(mime='text/html')
        with self.assertRaisesRegex(cover.CoverError, 'PNG, JPEG or WebP') as caught:
            cover.download_source(self.url, self.settings)
        self.assertEqual(caught.exception.stage, 'source_download')
        body.read.assert_not_called()
        body.close.assert_called_once_with()

    def test_s3_read_failure_still_closes_body_and_hides_details(self):
        from botocore.exceptions import ReadTimeoutError
        body = self.response_body()
        body.read.side_effect = ReadTimeoutError(endpoint_url=self.url, error='offline-secret')
        with self.assertRaises(cover.CoverError) as caught:
            cover.download_source(self.url, self.settings)
        self.assertEqual(caught.exception.stage, 'source_download')
        self.assertIn('timed out', str(caught.exception))
        self.assertNotIn(self.url, str(caught.exception))
        self.assertNotIn('offline-secret', str(caught.exception))
        body.close.assert_called_once_with()

    def test_s3_access_denied_reports_safe_status_without_body(self):
        from botocore.exceptions import ClientError
        self.aws.return_value.get_object.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'offline-secret private-response-body'},
             'ResponseMetadata': {'HTTPStatusCode': 403}}, 'GetObject')
        with self.assertRaises(cover.CoverError) as caught:
            cover.download_source(self.url, self.settings)
        self.assertEqual(caught.exception.stage, 'source_download')
        self.assertIn('HTTP 403', str(caught.exception))
        for secret in (self.url, 'offline-secret', 'private-response-body'):
            self.assertNotIn(secret, str(caught.exception))
        self.http.assert_not_called()

    def test_unexpected_body_failure_closed_and_stage_retained(self):
        body = self.response_body()
        body.read.side_effect = RuntimeError('offline-secret')
        with self.assertRaises(RuntimeError) as caught:
            cover.download_source(self.url, self.settings)
        self.assertEqual(caught.exception.stage, 'source_download')
        body.close.assert_called_once_with()
