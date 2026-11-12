"""Offline private recovery API and sync authentication tests; never app.main."""
import ast
import contextlib
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_instagram_event_extraction import serial
from app.routes import recovery_read as api, fomo_sync, instagram_pipeline
from app import service_auth
from app.recovery_contract import field_hash
from test_instagram_post_presentation import parent, children

READ='offline-read-token-'+'r'*32
SYNC='offline-sync-token-'+'s'*32


class RecoveryReadApiTests(unittest.TestCase):
    def setUp(self):
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ,{'PLAYGROUND_RECOVERY_READ_TOKEN':READ,'PLAYGROUND_SYNC_TOKEN':SYNC}))
        self.db=MagicMock()
        app=FastAPI();app.include_router(api.router)
        app.include_router(fomo_sync.router,prefix='/sync');app.include_router(instagram_pipeline.router,prefix='/sync')
        app.dependency_overrides[api.get_read_db]=lambda:self.db
        self.client=TestClient(app)
        self.headers={'Authorization':'Bearer '+READ}
        self.p=parent()
        self.p.post_url='https://www.instagram.com/p/example/'
        self.p.post_description='Parent summary';self.p.caption='caption'
        self.rows=children()
        self.source=self.stack.enter_context(patch.object(api,'source',return_value=(self.p,self.rows,[])))

    def test_read_routes_require_dedicated_token(self):
        for path in ('health','configuration','tasks','parents/6','parents/6/import-preview'):
            response=self.client.get('/internal/recovery/'+path)
            self.assertEqual(response.status_code,401)
        self.source.assert_not_called()
        self.db.execute.assert_not_called()
        response=self.client.get('/internal/recovery/health',headers={'Authorization':'Bearer '+SYNC})
        self.assertEqual(response.status_code,401)

    def test_missing_service_configuration_fails_closed(self):
        with patch.dict(os.environ,{'PLAYGROUND_RECOVERY_READ_TOKEN':''}):
            response=self.client.get('/internal/recovery/health',headers=self.headers)
            self.assertEqual(response.status_code,503)

    def test_private_projection_no_urls_html_or_child_cover_promotion(self):
        self.p.generated_image_url='https://private.s3.example/cover?X-Amz-Signature=secret'
        response=self.client.get('/internal/recovery/parents/6/import-preview',headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertEqual(data['projection_status'],'ready')
        self.assertEqual(data['parent']['image_source'],'generated_parent')
        self.assertEqual(len(data['parent']['activities']),2)
        self.assertEqual(len(data['expected_field_hashes']),10)
        for forbidden in ('https://','X-Amz','legacy-child-cover','lease_token','<p>'):
            self.assertNotIn(forbidden,response.text)
        self.db.commit.assert_not_called();self.db.add.assert_not_called();self.db.flush.assert_not_called()
        self.assertEqual(response.headers['cache-control'],'no-store')

    def test_configuration_is_allowlisted_and_invalid_caps_fail_closed(self):
        with patch.dict(os.environ,{'AWS_SECRET_ACCESS_KEY':'private-secret','INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS':'2'}):
            response=self.client.get('/internal/recovery/configuration',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('private-secret',response.text)
        self.assertEqual(response.json()['cover_attempt_cap'],2)
        with patch.dict(os.environ,{'INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS':'bad-secret'}):
            response=self.client.get('/internal/recovery/configuration',headers=self.headers)
        self.assertEqual(response.status_code,503)
        self.assertNotIn('bad-secret',response.text)

    def test_pagination_status_validation_and_no_writes(self):
        for query in ('page=0','page_size=101','status=bad','stage=bad'):
            self.assertEqual(self.client.get('/internal/recovery/tasks?'+query,headers=self.headers).status_code,422)
        self.db.scalar.return_value=0;self.db.scalars.return_value.all.return_value=[]
        response=self.client.get('/internal/recovery/tasks?page=2&page_size=5&status=failed',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['page'],2)
        stmt=str(self.db.scalars.call_args.args[0])
        self.assertIn('LIMIT',stmt);self.assertIn('OFFSET',stmt);self.assertIn('status',stmt)
        self.db.commit.assert_not_called();self.db.add.assert_not_called()

    def test_acknowledgment_is_computed_without_acknowledging(self):
        state=SimpleNamespace(instagram_post_id=6,stage='event_master',status='succeeded',attempt_count=1,
            failure_count=0,last_attempt_at=None,next_attempt_at=None,lease_expires_at=None,
            acknowledged_revision='a'*64,safe_reason=None)
        self.assertEqual(api.task_dto(state,'a'*64).acknowledgment,'current')
        self.assertEqual(api.task_dto(state,'b'*64).acknowledgment,'stale')
        state.acknowledged_revision=None
        self.assertEqual(api.task_dto(state,'a'*64).acknowledgment,'not_acknowledged')
        state.safe_reason='secret URL https://example/x'
        self.assertEqual(api.task_dto(state).reason,'other')

    def test_sync_routes_reject_unauthorized_and_read_only_token_before_actions(self):
        with patch.object(fomo_sync,'run_full_fomo_sync') as fomo,patch.object(instagram_pipeline,'run_instagram_scraper') as scrape:
            for url in ('/sync/fomo','/sync/instagram_pipeline'):
                self.assertEqual(self.client.post(url).status_code,401)
                self.assertEqual(self.client.post(url,headers=self.headers).status_code,401)
            fomo.assert_not_called();scrape.assert_not_called()

    def test_sync_credential_still_authorizes_existing_fomo_operation(self):
        result=SimpleNamespace(success=True,rows_sent=1,batches=1,error=None,failed_batch=None,http_status=200)
        with patch.object(fomo_sync,'fomo_sync_settings',return_value=('https://private?token=secret',None,None,None)), \
             patch.object(fomo_sync,'run_full_fomo_sync',return_value=result) as run:
            response=self.client.post('/sync/fomo',headers={'Authorization':'Bearer '+SYNC})
        self.assertEqual(response.status_code,200)
        run.assert_called_once()
        self.assertNotIn('token=secret',response.text)

    def test_authorized_sync_errors_are_sanitized(self):
        with patch.object(fomo_sync,'fomo_sync_settings',side_effect=RuntimeError('secret-signed-url')):
            response=self.client.post('/sync/fomo',headers={'Authorization':'Bearer '+SYNC})
        self.assertEqual(response.status_code,503)
        self.assertNotIn('secret-signed-url',response.text)

    def test_invalid_query_does_not_echo_signed_url(self):
        response=self.client.get('/internal/recovery/tasks?status=https://example.com?X-Amz-Signature=secret',headers=self.headers)
        self.assertEqual(response.status_code,422)
        self.assertNotIn('X-Amz',response.text)

    def test_reused_tokens_fail_closed_for_mutations(self):
        with patch.dict(os.environ,{'PLAYGROUND_SYNC_TOKEN':READ}):
            response=self.client.post('/sync/fomo',headers=self.headers)
        self.assertEqual(response.status_code,503)

    def test_no_private_action_routes_and_no_claim_code(self):
        for method in ('post','put','patch','delete'):
            self.assertEqual(getattr(self.client,method)('/internal/recovery/tasks',headers=self.headers).status_code,405)
        tree=ast.parse(Path(api.__file__).read_text())
        calls=[node.func.attr for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)]
        for forbidden in ('claim','finish','commit','add','flush','acknowledge','run'):
            self.assertNotIn(forbidden,calls)

    def test_database_dependency_sets_read_only_and_always_rolls_back(self):
        import Blueprint_db
        factory=MagicMock()
        session=factory.return_value.__enter__.return_value
        with patch.object(Blueprint_db,'SessionLocal',factory):
            generator=api.get_read_db();self.assertIs(next(generator),session)
            generator.close()
        self.assertIn('READ ONLY',str(session.execute.call_args.args[0]))
        session.rollback.assert_called_once();session.commit.assert_not_called()


if __name__=='__main__':unittest.main()
