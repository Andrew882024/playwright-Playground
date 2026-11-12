"""Offline regressions for parent invalidation, analysis accounting and safe protocol."""
import contextlib
import copy
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from test_instagram_event_reconciliation import MemorySession, incoming, child
from instagram_event_reconciliation import persist_analysis
from test_instagram_event_extraction import serial, ga, row_six, TZ
from app.event_master_sync.sync_service import _parent_result


class AuditFixTests(unittest.TestCase):
    def memory(self):
        values = incoming()
        session = MemorySession([child(values[0],11),child(values[1],22)])
        session.parent.generated_image_url = 'saved-image'
        session.parent.generated_image_input_hash = 'saved-hash'
        return session,values

    def test_parent_cover_preserved_for_reorder_and_invalidated_for_changes(self):
        for change in ('reorder','location','description','remove','add','source'):
            with self.subTest(change=change):
                session, values = self.memory()
                updates = {}
                if change == 'reorder': values.reverse()
                if change == 'location': values[0]['location'] = 'Different room'
                if change == 'description': values[0]['event_description'] = 'New content'
                if change == 'remove': values.pop()
                if change == 'add':
                    other=copy.deepcopy(values[0]);other['event_title']='Third session'; values.append(other)
                if change == 'source': updates['main_image_url']='new-source'
                persist_analysis(lambda:session,'acm','post',updates,values,reanalyze=True)
                self.assertEqual(session.parent.generated_image_url,'saved-image' if change=='reorder' else None)
                self.assertEqual(session.parent.generated_image_input_hash,'saved-hash' if change=='reorder' else None)

    def test_parent_invalidation_rolls_back_with_extraction(self):
        session,values=self.memory();session.fail_commit=True;values[0]['location']='Changed'
        with self.assertRaises(RuntimeError):
            persist_analysis(lambda:session,'acm','post',{},values,reanalyze=True)
        self.assertEqual(session.parent.generated_image_url,'saved-image')
        self.assertEqual(session.parent.generated_image_input_hash,'saved-hash')

    def test_no_candidates_programmatic_noop_preserves_cli_exit(self):
        stats={}
        with patch.object(serial,'gemini_api_key',return_value='offline'),patch.object(serial.genai,'Client'), \
             patch.object(serial,'distinct_profile_usernames_recent',return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            serial.main([],committed_parent_ids=set(),analysis_stats=stats)
            self.assertEqual(stats,dict(attempted=0,committed=0,failed=0))
            with self.assertRaises(SystemExit) as failure:serial.main([])
            self.assertEqual(failure.exception.code,1)

    def test_attempts_commits_and_failures_reported_separately(self):
        stats=dict(attempted=0,committed=0,failed=0)
        rows=[SimpleNamespace(id=1,post_shortcode='first'),SimpleNamespace(id=2,post_shortcode='second')]
        ai=ga.PostAnalysisAI.model_validate(row_six())
        committed=set()
        with patch.object(serial,'fetch_instagram_rows_for_profile',return_value=rows), \
             patch.object(serial,'build_analyzer_post_dict',return_value={}), \
             patch.object(serial,'analyze_post',side_effect=[RuntimeError('offline failure'),(ai,0)]), \
             patch.object(serial,'persist_analysis',return_value=True), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            serial.process_profile_db('acm',client=Mock(),model_chain=['offline'],tz=TZ,limit=None,
                only_not_analyzed=True,sleep_s=0,committed_parent_ids=committed,analysis_stats=stats)
        self.assertEqual(stats,dict(attempted=2,committed=1,failed=1))
        self.assertEqual(committed,{2})

    def test_allowlisted_importer_outcomes_only(self):
        for action in ('created','updated','unchanged','adopted'):
            data=dict(status='succeeded',action=action,event_id=15,post_id=1597)
            self.assertEqual(_parent_result(json.dumps(data).encode(),1597,0)['outcome'],action)
        for status,data,code in [('skipped',dict(reason='no_activities',event_id=None),0),
                                 ('adoption-required',dict(event_id=15),2),
                                 ('migration-conflict',dict(reason='legacy_child_imports',event_ids=[15]),2)]:
            self.assertEqual(_parent_result(json.dumps(dict(status=status,**data)).encode(),1597,code)['outcome'],status)
        for data in (dict(status='succeeded',action='created',event_id=15,post_id=1),
                     dict(status='skipped',reason='password=offline-secret'),
                     dict(status='succeeded',action='created',post_id=1597,event_id=15,description='offline-secret')):
            self.assertIsNone(_parent_result(json.dumps(data).encode(),1597,0))
        for raw in (b'offline-secret', b'{"status":"skipped","status":"skipped","reason":"no_activities"}', b'x'*9000):
            self.assertIsNone(_parent_result(raw,1597,0))

    def test_pipeline_noop_and_failed_analysis_statuses(self):
        import tempfile
        from pathlib import Path
        from app.routes import instagram_pipeline as route
        for failed in (0,1):
            def analyze(argv,*,committed_parent_ids,analysis_stats,**kwargs):
                analysis_stats.update(attempted=failed,committed=0,failed=failed)
            with tempfile.TemporaryDirectory() as tmp,patch.object(route,'PROJECT_ROOT',Path(tmp)), \
                 patch.object(route,'run_instagram_scraper'),patch.object(route,'ai_analyze_main',side_effect=analyze), \
                 patch.object(route,'run_add_our_own_s3_url'), \
                 patch.object(route,'run_optional_cover_stage',return_value={'status':'completed'}) as cover, \
                 patch.object(route,'run_optional_event_master_sync',return_value={'status':'completed'}) as sync, \
                 patch.dict(os.environ,{'INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS':'1'}), \
                 contextlib.redirect_stdout(io.StringIO()),patch('sys.__stdout__',io.StringIO()):
                result=route.post_instagram_pipeline()
            self.assertEqual(result['analysis']['status'],'failed' if failed else 'no-op')
            cover.assert_called_once_with(set());sync.assert_called_once_with(set())
