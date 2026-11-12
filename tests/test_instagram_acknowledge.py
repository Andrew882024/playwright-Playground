"""Offline acknowledgment-only safety regressions."""
import contextlib
import io
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as Row
from unittest.mock import Mock,patch,MagicMock
from test_instagram_event_extraction import serial
from app.pipeline_recovery import acknowledge as service
import acknowledge_instagram_parent_import as cli


class AcknowledgeTests(unittest.TestCase):
    def verified(self):return dict(status='verified',event_id=15,post_id=1597,source_revision='a'*64)
    def store(self,state):
        session=MagicMock();session.__enter__.return_value=session
        session.execute.return_value.scalar_one.return_value=state
        store=Mock();store.factory.return_value=session;store.snapshot.return_value=(Row(),[])
        return store,session
    def state(self,**kwargs):
        return Row(lease_expires_at=None,status='pending',source_revision='old',acknowledged_revision=None,**kwargs)

    def test_dry_run_no_state_reads_or_writes(self):
        store=Mock();verifier=Mock(return_value=self.verified())
        result=service.acknowledge(1597,store=store,verifier=verifier)
        self.assertEqual(result['status'],'dry-run');self.assertEqual(result['event_id'],15)
        store.factory.assert_not_called();self.assertEqual(verifier.call_count,2)

    def test_dry_run_changed_snapshot_rejected(self):
        other=self.verified()|dict(source_revision='b'*64)
        with self.assertRaisesRegex(service.AcknowledgmentError,'verification_changed'):
            service.acknowledge(1597,verifier=Mock(side_effect=[self.verified(),other]))

    def test_acknowledgment_writes_only_recovery_row_and_is_idempotent(self):
        state=self.state();store,session=self.store(state)
        with patch('app.pipeline_recovery.store.revision',return_value='a'*64):
            result=service.acknowledge(1597,write=True,store=store,verifier=Mock(return_value=self.verified()))
            self.assertEqual(result['action'],'acknowledged')
            self.assertEqual(state.acknowledged_revision,'a'*64)
            again=service.acknowledge(1597,write=True,store=store,verifier=Mock(return_value=self.verified()))
            self.assertEqual(again['action'],'already_acknowledged')
        session.add.assert_not_called()
        for call in session.execute.call_args_list:
            self.assertIn('instagram_pipeline_recovery',str(call.args[0]))
        store.snapshot.assert_called_with(session,1597,lock=True)

    def test_stale_revision_and_active_lease_reject_before_ack(self):
        for rev,leased in (('b'*64,False),('a'*64,True)):
            state=self.state();store,session=self.store(state)
            if leased:state.lease_expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)
            with patch('app.pipeline_recovery.store.revision',return_value=rev),self.assertRaises(service.AcknowledgmentError):
                service.acknowledge(1597,write=True,store=store,verifier=Mock(return_value=self.verified()))
            self.assertIsNone(state.acknowledged_revision)

    def test_cli_defaults_to_dry_run_and_explicit_flag_only(self):
        with patch('dotenv.load_dotenv'),patch.object(service,'acknowledge',return_value=dict(status='dry-run')) as call,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--post-id','1597']),0);call.assert_called_once_with(1597,write=False)
            call.reset_mock();cli.main(['--post-id','1597','--acknowledge']);call.assert_called_once_with(1597,write=True)

    def test_raw_exception_never_printed(self):
        with patch('dotenv.load_dotenv'),patch.object(service,'acknowledge',side_effect=RuntimeError('offline-secret')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['--post-id','1597']),1)
        self.assertNotIn('offline-secret',output.getvalue())
