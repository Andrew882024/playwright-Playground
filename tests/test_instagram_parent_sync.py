"""Offline parent sync boundaries: subprocesses and pipeline stages are mocked."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_instagram_event_extraction import serial
from app.event_master_sync import sync_service as sync
from app.event_cover_service import pipeline_stage as covers
from app.routes import instagram_pipeline as pipeline
import sync_instagram_posts_to_event_master as cli


class ParentSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        python=root/'venv/bin/python';python.parent.mkdir(parents=True);python.write_text('offline');python.chmod(0o700)
        (root/'import_instagram_event.py').write_text('# offline, never executed')
        self.settings=sync.SyncSettings(root,python)

    def test_five_children_one_parent_one_subprocess(self):
        with patch.object(sync,'children_for_parents',return_value=[1,2,3,4,5]) as child_query, \
             patch.object(sync.SyncSettings,'from_env',return_value=self.settings), \
             patch.object(sync.subprocess,'run',return_value=SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='succeeded', action='unchanged', post_id=6, event_id=15)).encode())) as run, \
             patch.dict(os.environ,{'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER':'true'}), \
             contextlib.redirect_stdout(io.StringIO()):
            result=sync.run_optional_event_master_sync({6})
        child_query.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-3:],['--instagram-post-id','6','--sync'])
        self.assertEqual(result['attempted'],1)
        self.assertNotIn('--adopt-event-id',run.call_args.args[0])
        self.assertNotIn('DB_PASSWORD',run.call_args.kwargs['env'])
        self.assertIn('INSTAGRAM_PLAYGROUND_DIR',run.call_args.kwargs['env'])

    def test_parent_ids_deduplicated_and_conflicts_are_safe(self):
        with patch.object(sync.subprocess,'run',side_effect=[SimpleNamespace(returncode=2,stderr=b'secret',stdout=b'{"status":"adoption-required","event_id":12}'),
                                                         SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='succeeded', action='unchanged', post_id=7, event_id=15)).encode())]) as run:
            result=sync.sync_posts([6,6,7],self.settings)
        self.assertEqual(run.call_count,2)
        self.assertEqual(result['parents'][0]['status'],'adoption-required')
        self.assertEqual(result['parents'][1]['status'],'succeeded')
        self.assertNotIn('secret',json.dumps(result))

    def test_explicit_adoption_only_forwarded_when_requested(self):
        with patch.object(sync.subprocess,'run',return_value=SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='succeeded', action='unchanged', post_id=6, event_id=15)).encode())) as run:
            sync.sync_posts([6],self.settings,adopt_event_id=12)
        self.assertEqual(run.call_args.args[0][-2:],['--adopt-event-id','12'])
        with patch.object(sync.subprocess,'run') as run:
            self.assertEqual(sync.sync_posts([6,7],self.settings,adopt_event_id=12)['status'],'error')
        run.assert_not_called()

    def test_cli_dry_run_never_invokes_importer_or_database(self):
        with patch.object(sync,'sync_posts') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['--post-id','6']),0)
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['status'],'dry-run')
        with patch.object(sync,'sync_posts',return_value={'status':'completed'}) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--post-id','6','--sync']),0)
        run.assert_called_once_with([6],adopt_event_id=None)

    def test_cover_failure_does_not_block_one_parent_import(self):
        def analyze(argv,*,committed_parent_ids,analysis_stats=None):committed_parent_ids.add(6)
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline,'PROJECT_ROOT',Path(tmp)), \
             patch.object(pipeline,'run_instagram_scraper'),patch.object(pipeline,'ai_analyze_main',side_effect=analyze), \
             patch.object(pipeline,'run_add_our_own_s3_url'), \
             patch.object(covers,'parents_for_posts',return_value=[(6,None,'original',None)]), \
             patch.object(covers.Settings,'from_env',return_value=Mock()), \
             patch.object(covers,'run_cover',side_effect=RuntimeError('offline cover failure')) as cover, \
             patch.object(sync.SyncSettings,'from_env',return_value=self.settings), \
             patch.object(sync.subprocess,'run',return_value=SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='succeeded', action='unchanged', post_id=6, event_id=15)).encode())) as run, \
             patch.dict(os.environ,{'INSTAGRAM_PIPELINE_GENERATE_COVERS':'true',
                                   'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS':'1',
                                   'INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER':'true',
                                   'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS':''}), \
             contextlib.redirect_stdout(io.StringIO()):
            result=pipeline.post_instagram_pipeline()
        cover.assert_called_once();run.assert_called_once()
        self.assertEqual(result['cover_stage']['failed'],1)
        self.assertEqual(result['event_master_sync']['attempted'],1)
        self.assertEqual(result['event_master_sync']['succeeded'],1)
        self.assertTrue(result['success'])

    def test_allowlisted_outcome_counts_and_unknown_output_never_logged(self):
        replies=[]
        for i,action in enumerate(('created','updated','unchanged'),1):
            replies.append(SimpleNamespace(returncode=0,stdout=json.dumps(dict(
                status='succeeded',action=action,post_id=i,event_id=10+i)).encode()))
        replies.extend([
            SimpleNamespace(returncode=0,stdout=b'{"status":"skipped","reason":"no_activities","event_id":null}'),
            SimpleNamespace(returncode=2,stdout=b'{"status":"adoption-required","event_id":15}'),
            SimpleNamespace(returncode=2,stdout=b'{"status":"migration-conflict","reason":"legacy_child_imports","event_ids":[16,17]}'),
            SimpleNamespace(returncode=0,stdout=b'{"status":"succeeded","action":"created","post_id":7,"event_id":20,"description":"offline-secret"}',stderr=b'offline-secret'),
        ])
        with patch.object(sync.subprocess,'run',side_effect=replies) as run,contextlib.redirect_stdout(io.StringIO()) as output:
            result=sync.sync_posts(range(1,8),self.settings)
        for action in ('created','updated','unchanged','skipped','adoption-required','migration-conflict'):
            self.assertEqual(result['outcomes'][action],1)
        self.assertEqual(result['failed'],3)
        self.assertEqual(result['succeeded'],4)
        self.assertEqual(result['parents'][-1]['reason'],'importer_result_invalid')
        self.assertNotIn('offline-secret',json.dumps(result)+output.getvalue())
        self.assertEqual(run.call_args.kwargs['stdout'],sync.subprocess.PIPE)
        self.assertIs(run.call_args.kwargs['shell'],False)
