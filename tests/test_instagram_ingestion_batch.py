"""Offline automatic-binding orchestration; providers and parent writes mocked."""
from contextlib import redirect_stdout,redirect_stderr,contextmanager
from pathlib import Path
import io,json,tempfile,unittest
from unittest.mock import Mock,patch

from app.instagram_scraper import ingestion_batch as batch
import collect_instagram_posts as cli


class IngestionBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.records=[dict(index=i,account=a,shortcode=f'synthetic{i}',permalink=f'https://www.instagram.com/p/synthetic{i}/',
            uncertainty_reasons=['source_verification_unavailable']) for i,a in enumerate(('rco.ucsd','tritongamingsd','rco.ucsd'),1)]
        self.prepared=dict(batch_id='a'*64,candidates=self.records,exclusion_counts=dict(skipped_expired=35,skipped_already_imported=3),duplicate_observations_removed=1)
        self.ledger=Mock();self.ledger.summary.return_value=dict(status='available',providers={'gemini':dict(limit=20,used=0,remaining=20),'openai_cover':dict(limit=3,used=0,remaining=3)})
        self.patches=[patch.object(batch.analysis_batch,'prepare_batch',return_value=self.prepared),
            patch.object(batch.parent_binding,'preview_record',side_effect=lambda root,r:dict(parent_id=None,status='creation_required')),
            patch.object(batch.parent_binding,'bind_candidate',side_effect=lambda root,b,i,**kw:dict(parent_id=1800+i,analysis_batch_id=str(i)*64,database_writes=1)),
            patch.object(batch.analysis_execution,'execute',return_value=dict(status='analyzed_locally',gemini_http_requests=1)),
            patch.object(batch.daily_budget,'Ledger',return_value=self.ledger),
            patch.object(batch,'single_run_lock',side_effect=self.lock)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    @staticmethod
    @contextmanager
    def lock(path):yield True
    def execute(self,run=False,**kwargs):
        opts=dict(approve_parent_writes=True,approve_analysis_only=True,confirm_source_unverified=True,confirm_paid_requests=True) if run else {}
        opts.update(kwargs)
        return batch.execute(self.root,run=run,**opts)
    def test_readonly_dry_run_across_all_accounts(self):
        result=self.execute()
        self.assertEqual(len(result['profiles']),16);self.assertEqual(result['parents_would_be_created'],3)
        self.assertEqual(result['ready_after_binding_for_approved_analysis'],3)
        self.assertEqual(result['ready_for_paid_now'],0);self.assertEqual(result['blocked_from_production_import'],3)
        self.assertEqual(result['exclusion_counts']['skipped_expired'],35)
        self.assertEqual(result['exclusion_counts']['skipped_already_imported'],3)
        batch.parent_binding.bind_candidate.assert_not_called();batch.analysis_execution.execute.assert_not_called()
        self.assertFalse((self.root/'analysis-batches').exists())
        self.assertIs(batch.analysis_batch.prepare_batch.call_args.kwargs['live_parent_lookup'],batch.parent_binding.lookup_parents)
    def test_automatic_binding_then_existing_executor_no_manual_ids(self):
        result=self.execute(True)
        self.assertEqual(result['parents_created'],1);self.assertEqual(result['gemini_http_requests'],1)
        self.assertEqual(result['skipped'],2)
        batch.parent_binding.bind_candidate.assert_called_once_with(self.root,'a'*64,1,write=True)
        batch.analysis_execution.execute.assert_called_once_with(self.root,'1'*64,1801,run=True)
        for item in result['candidates']:self.assertFalse(item['import_authorized'])
        self.assertEqual(result['openai_requests'],0);self.assertEqual(result['production_imports'],0)
    def test_reuses_existing_parent(self):
        batch.parent_binding.preview_record.side_effect=lambda root,r:dict(parent_id=1800+r['index'],status='existing_parent')
        batch.parent_binding.bind_candidate.side_effect=lambda root,b,i,**kw:dict(parent_id=1800+i,analysis_batch_id=str(i)*64,database_writes=0)
        result=self.execute(True)
        self.assertEqual(result['parents_reused'],1);self.assertEqual(result['parents_would_be_reused'],3);self.assertEqual(result['parents_created'],0)
        self.assertEqual(result['database_writes'],0)
    def test_three_explicit_requests_share_global_budget(self):
        result=self.execute(True,max_http_requests=2)
        self.assertEqual(result['gemini_http_requests'],2);self.assertEqual(batch.analysis_execution.execute.call_count,2)
        self.assertEqual(batch.parent_binding.bind_candidate.call_count,2)
    def test_unknown_execution_failure_still_consumes_batch_slot(self):
        batch.analysis_execution.execute.side_effect=RuntimeError('PRIVATE_URL')
        result=self.execute(True)
        self.assertEqual(result['gemini_http_slots_reserved'],1)
        self.assertTrue(result['gemini_dispatch_count_uncertain'])
        batch.analysis_execution.execute.assert_called_once()
        self.assertNotIn('PRIVATE_URL',json.dumps(result))
    def test_failed_parent_does_not_get_a_second_request(self):
        batch.analysis_execution.execute.return_value=dict(status='review_required',gemini_http_requests=1,diagnostics={'http_status':503})
        result=self.execute(True)
        self.assertEqual(result['review_required'],1);self.assertEqual(result['gemini_http_requests'],1)
        batch.analysis_execution.execute.assert_called_once()
    def test_429_stops_batch_without_retry_even_when_local_allowance_remains(self):
        batch.analysis_execution.execute.return_value=dict(status='review_required',gemini_http_requests=1,diagnostics={'http_status':429})
        result=self.execute(True,max_http_requests=3)
        self.assertEqual(result['gemini_http_requests'],1)
        self.assertEqual(result['candidates'][1]['reason'],'provider_rate_limited_no_automatic_retry')
        batch.analysis_execution.execute.assert_called_once()
    def test_zero_paid_cap_binds_without_dispatch(self):
        result=self.execute(True,max_http_requests=0,confirm_paid_requests=False)
        self.assertEqual(result['parents_created'],3);self.assertEqual(result['gemini_http_requests'],0)
        batch.analysis_execution.execute.assert_not_called()
    def test_no_remaining_daily_budget_creates_no_parents_or_requests(self):
        self.ledger.summary.return_value['providers']['gemini']['remaining']=0
        result=self.execute(True)
        self.assertEqual(result['ready_for_paid_now'],0);self.assertEqual(result['skipped'],3)
        batch.parent_binding.bind_candidate.assert_not_called();batch.analysis_execution.execute.assert_not_called()
    def test_live_journal_or_identity_block_is_isolated(self):
        batch.parent_binding.preview_record.side_effect=[batch.parent_binding.Blocked('previous_analysis_outcome_requires_review'),
            dict(parent_id=None,status='creation_required'),dict(parent_id=None,status='creation_required')]
        result=self.execute(True)
        self.assertEqual(result['review_required'],1);self.assertEqual(result['parents_created'],1)
        self.assertEqual(batch.parent_binding.bind_candidate.call_args.args[2],2)
    def test_missing_approvals_fail_before_binding(self):
        with self.assertRaises(ValueError):batch.execute(self.root,run=True)
        batch.parent_binding.bind_candidate.assert_not_called()
    def test_overlapping_run_is_rejected(self):
        @contextmanager
        def busy(path):yield False
        with patch.object(batch,'single_run_lock',side_effect=busy):result=self.execute(True)
        self.assertEqual(result['status'],'busy');batch.analysis_execution.execute.assert_not_called()


class CollectorIngestionCLITests(unittest.TestCase):
    def test_existing_collector_entrypoint_staged_mode_does_not_scrape(self):
        with patch.object(batch,'execute',return_value=dict(status='dry_run')) as execute, \
                patch.object(cli,'collect_profiles') as collect,redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--all-profiles','--ingest-staged']),0)
        collect.assert_not_called();self.assertEqual(len(execute.call_args.kwargs['profiles']),16)
        self.assertFalse(execute.call_args.kwargs['run'])
    def test_cli_requires_explicit_approvals(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            cli.main(['--all-profiles','--ingest-staged','--run'])
    def test_outer_failure_does_not_claim_zero_paid_requests(self):
        output=io.StringIO()
        with patch.object(batch,'execute',side_effect=RuntimeError('PRIVATE_BODY')),redirect_stdout(output):
            self.assertEqual(cli.main(['--all-profiles','--ingest-staged','--run','--approve-parent-writes',
                '--approve-analysis-only','--confirm-source-unverified','--confirm-paid-requests']),1)
        result=json.loads(output.getvalue())
        self.assertIsNone(result['gemini_http_requests'])
        self.assertTrue(result['gemini_dispatch_count_uncertain'])
        self.assertNotIn('PRIVATE_BODY',output.getvalue())
    def test_collection_failure_never_starts_binding_or_analysis(self):
        with patch.object(batch,'execute') as execute,patch.object(cli,'collect_profiles',return_value=dict(status='failed')),redirect_stdout(io.StringIO()):
            code=cli.main(['--all-profiles','--collect-and-ingest','--run','--approve-parent-writes',
                '--approve-analysis-only','--confirm-source-unverified','--confirm-paid-requests'])
        self.assertEqual(code,1);execute.assert_not_called()


class LiveSelectionTests(unittest.TestCase):
    def test_failed_live_journal_does_not_crowd_out_next_candidate(self):
        import test_instagram_analysis_batch as fixtures
        from app.instagram_scraper.analysis_batch import prepare_batch
        case=fixtures.AnalysisBatchTests();case.setUp();self.addCleanup(case.doCleanups)
        case.add('rco.ucsd',1);case.add('tritongamingsd',2)
        result=prepare_batch(case.root,limit=1,now=fixtures.NOW,budget_snapshot=fixtures.BUDGET,
            live_parent_lookup=lambda code:[dict(parent_id=1645,account='rco.ucsd',analyzed=False,has_activities=False,has_import_ack=False)] if code=='synthetic1' else [],
            journal_inspector=lambda p:dict(review_required=p==1645))
        self.assertEqual(result['selected_count'],1)
        self.assertEqual(result['candidates'][0]['shortcode'],'synthetic2')
        self.assertEqual(result['exclusion_counts']['prior_paid_attempt_requires_separate_retry_approval'],1)
    def test_profile_filter_uses_existing_shared_selection(self):
        import test_instagram_analysis_batch as fixtures
        case=fixtures.AnalysisBatchTests();case.setUp();self.addCleanup(case.doCleanups)
        case.add('rco.ucsd',1);case.add('tritongamingsd',2)
        result=case.prepare(profiles=['tritongamingsd'])
        self.assertEqual([r['account'] for r in result['candidates']],['tritongamingsd'])
    def test_old_parent_binding_cannot_be_silently_replaced(self):
        from app.instagram_scraper import parent_binding
        with self.assertRaisesRegex(parent_binding.Blocked,'canonical_parent_binding_missing'):
            parent_binding.check_binding(dict(parent_id=1645),{},[])
