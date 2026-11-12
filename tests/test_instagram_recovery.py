"""Offline recovery orchestration, claim policy, budgets and revision regressions."""
import contextlib
import io
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from unittest.mock import Mock, patch

from test_instagram_event_extraction import serial
from app.pipeline_recovery.service import RecoveryRun, run_recovery
from app.pipeline_recovery import hooks
from app.pipeline_recovery.store import blocked, Store
from app.event_cover_service.openai_cover import CoverError
from app.instagram_post_presentation import import_source_revision
from test_instagram_post_presentation import parent, children


class MemoryStore:
    def __init__(self):
        self.records={};self.claims=[];self.finishes=[]
    def candidates(self,stage,excluded,**kwargs):
        return [dict(parent_id=i,reason='incomplete') for i in range(1,4) if i not in excluded]
    def claim(self,stage,i):
        if (stage,i) in self.records:return None
        claim=dict(stage=stage,parent_id=i,token='token',revision='a'*64)
        self.records[stage,i]=claim;self.claims.append(claim);return claim
    def finish(self,claim,status,reason,acknowledged=None):
        self.finishes.append((claim,status,reason,acknowledged));return status


class RecoveryTests(unittest.TestCase):
    def run_object(self,cover=True,imports=True,cap=1):
        return RecoveryRun(MemoryStore(),1,dict(cover=cap,event_master=cap),dict(cover=cover,event_master=imports))

    def test_disabled_invokes_nothing(self):
        run=self.run_object(False,False);run.store=Mock()
        self.assertEqual(run.recover('cover')['status'],'disabled')
        self.assertEqual(run.recover('event_master')['status'],'disabled');run.store.candidates.assert_not_called()

    def test_attempt_caps_shared_budget_and_saved_artifact_resume(self):
        run=self.run_object(cap=2);run.cover_allowance_used=1
        with patch('app.event_cover_service.parent_cover.run_parent_cover',return_value=dict(mode='resumed',api_calls=0)) as cover, \
             patch('app.event_cover_service.openai_cover.Settings.from_env'),contextlib.redirect_stdout(io.StringIO()):
            result=run.recover('cover')
        self.assertEqual(result['attempted'],2);self.assertEqual(result['recovered'],2)
        self.assertTrue(all(not c.kwargs['allow_generation'] for c in cover.call_args_list))
        self.assertEqual(result['skipped'],1)

    def test_remaining_allowance_authorizes_only_one_parent(self):
        run=self.run_object(cap=2)
        def generate(*args,**kwargs):
            if kwargs['allow_generation']:
                kwargs['generation_guard'](Row(context='{"activities":[{"end":"2099-01-01T00:00:00+00:00"}]}'),reserve=True)
                return dict(mode='generated',api_calls=1)
            return dict(mode='resumed',api_calls=0)
        with patch('app.event_cover_service.parent_cover.run_parent_cover',side_effect=generate) as cover, \
             patch('app.event_cover_service.openai_cover.Settings.from_env'),contextlib.redirect_stdout(io.StringIO()):
            run.recover('cover')
        self.assertEqual([c.kwargs['allow_generation'] for c in cover.call_args_list],[True,False])

    def test_ambiguous_requests_require_review_never_repeat(self):
        run=self.run_object()
        with patch('app.event_cover_service.parent_cover.run_parent_cover',side_effect=CoverError('Safe message',reason='review_required')) as cover, \
             patch('app.event_cover_service.openai_cover.Settings.from_env'),contextlib.redirect_stdout(io.StringIO()):
            result=run.recover('cover');run.recover('cover')
        self.assertEqual(result['review_required'],1)
        # Neither the same parent nor an extra recovery attempt can run again.
        self.assertEqual([c.args[1] for c in cover.call_args_list],[1])
        self.assertTrue(all(entry[1]=='review_required' for entry in run.store.finishes))

    def test_legacy_conflicts_never_adopt_and_one_failure_isolated(self):
        run=self.run_object(cap=2)
        with patch('app.event_master_sync.sync_service.sync_posts',side_effect=[
             dict(parents=[dict(status='adoption-required',event_id=15)]),
             dict(parents=[dict(status='succeeded',action='unchanged',source_revision='a'*64,event_id=15)])]) as sync, \
             contextlib.redirect_stdout(io.StringIO()):
            result=run.recover('event_master')
        self.assertEqual(result['review_required'],1);self.assertEqual(result['recovered'],1)
        self.assertEqual(sync.call_args_list[0].args,([1],));self.assertEqual(sync.call_args_list[0].kwargs,{})

    def test_normal_stage_exclusion_and_lease_claim(self):
        run=self.run_object()
        with run_recovery(run):
            self.assertTrue(hooks.begin('cover',1));self.assertFalse(hooks.begin('cover',1))
            hooks.finish('cover',1,dict(mode='generated'))
        with patch('app.event_cover_service.parent_cover.run_parent_cover',return_value=dict(mode='resumed')), \
             patch('app.event_cover_service.openai_cover.Settings.from_env'),contextlib.redirect_stdout(io.StringIO()):
            run.recover('cover')
        self.assertEqual([c['parent_id'] for c in run.store.claims],[1,2])

    def test_preview_is_select_only(self):
        run=self.run_object();run.store=Mock();run.store.candidates.return_value=[]
        run.preview();run.store.claim.assert_not_called();run.store.finish.assert_not_called()
        self.assertTrue(all(c.kwargs['preview'] for c in run.store.candidates.call_args_list))

    def test_revision_changes_for_cover_context_and_ignores_child_order(self):
        p=parent();rows=children();before=import_source_revision(p,rows)
        self.assertEqual(import_source_revision(p,rows[::-1]),before)
        p.generated_image_url='new-cover';self.assertNotEqual(import_source_revision(p,rows),before)
        p.generated_image_url=None;rows[0].event_title='Changed';self.assertNotEqual(import_source_revision(p,rows),before)

    def test_lease_cooldown_and_retry_limit_policy(self):
        now=datetime.now(timezone.utc)
        state=Row(status='failed',source_revision='a',acknowledged_revision=None,failure_count=1,
                  lease_expires_at=now+timedelta(minutes=1),next_attempt_at=None)
        self.assertEqual(blocked(state,'a',now,5),'leased')
        state.lease_expires_at=None;state.next_attempt_at=now+timedelta(minutes=15)
        self.assertEqual(blocked(state,'a',now,5),'cooldown')
        self.assertIsNone(blocked(state,'b',now,5))
        state.failure_count=5;self.assertEqual(blocked(state,'a',now,5),'retry_limit')
        state.status='review_required';self.assertEqual(blocked(state,'b',now,5),'review_required')

    def test_stale_revision_and_replaced_lease_cannot_acknowledge_success(self):
        claim=dict(parent_id=1,stage='event_master',token='owner',revision='a'*64)
        state=Row(lease_token='owner',failure_count=0)
        session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        tx=Mock();tx.__enter__=Mock();tx.__exit__=Mock(return_value=False);session.begin.return_value=tx
        session.execute.return_value.scalar_one.return_value=state
        store=Store(lambda:session,datetime.now(timezone.utc))
        with patch.object(store,'snapshot',return_value=(parent(),children())), \
             patch('app.pipeline_recovery.store.revision',return_value='b'*64):
            self.assertEqual(store.finish(claim,'succeeded',None,'a'*64),'failed')
        self.assertEqual(state.safe_reason,'stale_revision');self.assertFalse(hasattr(state,'acknowledged_revision'))
        state.lease_token='new-owner'
        with patch.object(store,'snapshot',return_value=None):
            self.assertEqual(store.finish(claim,'succeeded',None,'a'*64),'lease_lost')

    def test_two_runs_cannot_claim_same_stage_parent(self):
        first=self.run_object();second=self.run_object();second.store=first.store
        self.assertTrue(first.begin_normal('event_master',1))
        self.assertFalse(second.begin_normal('event_master',1))
        self.assertTrue(second.begin_normal('cover',1))  # Separate stage identity.

    def test_successful_ack_requires_exact_current_snapshot(self):
        claim=dict(parent_id=1,stage='event_master',token='owner',revision='a'*64)
        for acknowledged,expected in (('a'*64,'succeeded'),(None,'failed'),('b'*64,'failed')):
            state=Row(lease_token='owner',failure_count=0)
            session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
            tx=Mock();tx.__enter__=Mock();tx.__exit__=Mock(return_value=False);session.begin.return_value=tx
            session.execute.return_value.scalar_one.return_value=state
            store=Store(lambda:session,datetime.now(timezone.utc))
            with patch.object(store,'snapshot',return_value=(parent(),children())), \
                 patch('app.pipeline_recovery.store.revision',return_value='a'*64):
                self.assertEqual(store.finish(claim,'succeeded',None,acknowledged),expected)
            if expected=='succeeded':self.assertEqual(state.acknowledged_revision,'a'*64)

    def test_migration_creates_only_recovery_table_and_preview_has_no_writes(self):
        import create_instagram_pipeline_recovery_table as migration
        import Blueprint_db
        with patch.object(Blueprint_db.InstagramPipelineRecovery.__table__,'create') as create, \
             contextlib.redirect_stdout(io.StringIO()):
            migration.main()
        create.assert_called_once_with(bind=Blueprint_db.engine,checkfirst=True)
        self.assertEqual([c.name for c in Blueprint_db.InstagramPipelineRecovery.__table__.primary_key],
                         ['instagram_post_id','stage'])

    def test_preview_does_not_invoke_pipeline_cover_or_import(self):
        import preview_instagram_pipeline_recovery as preview
        run=self.run_object();run.store=Mock();run.store.candidates.return_value=[]
        with patch('app.pipeline_recovery.service.RecoveryRun.configured',return_value=run), \
             patch('dotenv.load_dotenv'), \
             patch('app.event_cover_service.parent_cover.run_parent_cover') as cover, \
             patch('app.event_master_sync.sync_service.sync_posts') as sync, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(preview.main(),0)
        cover.assert_not_called();sync.assert_not_called();run.store.claim.assert_not_called()

    def test_preview_uses_readonly_transaction_and_never_commits(self):
        session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        session.scalars.return_value.all.return_value=[]
        store=Store(lambda:session,datetime.now(timezone.utc))
        self.assertEqual(store.candidates('event_master',set(),preview=True),[])
        self.assertEqual(str(session.execute.call_args.args[0]),'SET TRANSACTION READ ONLY')
        session.commit.assert_not_called()
        sql=str(session.scalars.call_args.args[0])
        self.assertIn('last_checked_at',sql)
        self.assertIn('instagram_posts.ai_analyzed IS true',sql)
        self.assertIn('EXISTS',sql)
        self.assertIn('coalesce',sql)

    def test_recovery_existing_import_preserves_event_id(self):
        run=self.run_object()
        run.store.candidates=lambda *a,**kw:[dict(parent_id=1597,reason='incomplete')]
        with patch('app.event_master_sync.sync_service.sync_posts',return_value=dict(parents=[
                dict(status='succeeded',action='unchanged',event_id=15,post_id=1597,source_revision='a'*64)])) as sync, \
             contextlib.redirect_stdout(io.StringIO()):
            result=run.recover('event_master')
        self.assertEqual(result['recovered'],1)
        # Recovery never passes an adoption ID or a MySQL primary key to the importer.
        self.assertEqual(sync.call_args.kwargs,{})
        self.assertEqual(run.store.finishes[0][3],'a'*64)

    def test_unavailable_paid_budget_is_a_skip_not_a_failed_paid_request(self):
        run=self.run_object();run.cover_allowance_used=1
        with patch('app.event_cover_service.parent_cover.run_parent_cover',side_effect=CoverError(
                'Safe message',reason='generation_not_authorized')), \
             patch('app.event_cover_service.openai_cover.Settings.from_env'),contextlib.redirect_stdout(io.StringIO()):
            result=run.recover('cover')
        self.assertEqual(result['attempted'],1);self.assertEqual(result['skipped'],3);self.assertEqual(result['failed'],0)
        self.assertEqual(run.store.finishes[0][1],'pending')

    def test_cache_preview_reports_ambiguous_intent_without_modifying_artifacts(self):
        import json
        import tempfile
        from pathlib import Path
        from app.pipeline_recovery.service import inspect_cache
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp);directory=cache/'parents/1597/hash/request';directory.mkdir(parents=True)
            manifest=directory/'manifest.json';manifest.write_text(json.dumps(dict(state='request_started')))
            before=manifest.read_bytes()
            result=inspect_cache(cache,1597)
            self.assertEqual(result['reason'],'review_required')
            self.assertEqual(manifest.read_bytes(),before)
            self.assertFalse((directory/'generated.png').exists())

    def test_acknowledged_parent_is_separate_from_actionable_preview(self):
        run=self.run_object();run.store=Mock()
        acknowledged=dict(parent_id=1597,reason='already_current',expected_action='idempotent_parent_sync')
        legacy=[dict(parent_id=i,reason='incomplete',expected_action='idempotent_parent_sync') for i in (6,289)]
        run.store.candidates.side_effect=[[],legacy+[acknowledged]]
        preview=run.preview()
        self.assertEqual([p['parent_id'] for p in preview['event_master']],[6,289])
        self.assertEqual(preview['already_synchronized'],[dict(parent_id=1597,stage='event_master')])
        self.assertEqual(acknowledged['reason'],'already_current')
        self.assertEqual(run.recovery_attempts['event_master'],0)
        run.store.claim.assert_not_called();run.store.finish.assert_not_called()

    def test_already_current_does_not_consume_import_cap_or_lease(self):
        run=self.run_object();run.store=Mock()
        run.store.candidates.return_value=[dict(parent_id=1597,reason='already_current'),dict(parent_id=6,reason='incomplete')]
        claim=dict(parent_id=6,stage='event_master',token='offline',revision='a'*64)
        run.store.claim.return_value=claim;run.store.finish.return_value='review_required'
        with patch('app.event_master_sync.sync_service.sync_posts',return_value=dict(parents=[
                dict(status='adoption-required',event_id=12)])) as sync,contextlib.redirect_stdout(io.StringIO()):
            result=run.recover('event_master')
        self.assertEqual(result['attempted'],1)
        self.assertEqual(run.recovery_attempts['event_master'],1)
        run.store.claim.assert_called_once_with('event_master',6)
        sync.assert_called_once_with([6])  # No adoption flag, no call for parent 1597.
        self.assertEqual(result['review_required'],1)
        self.assertNotIn(1597,run.attempted['event_master'])
