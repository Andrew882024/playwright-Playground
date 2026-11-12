"""Offline binding and quarantine safety. Never connect to real databases."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock,patch

from test_instagram_event_extraction import serial
from test_instagram_daily_images import observed
from app.instagram_scraper import parent_binding as binding
from app.instagram_scraper import scrape_db as scrape
from app.instagram_scraper.analysis_batch import analyzer_input
from app.instagram_scraper.analysis_execution import load_input
from app.instagram_scraper.collection_staging import atomic,digest,private_dir
import bind_instagram_analysis_batch as cli


class ParentBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=private_dir(Path(self.tmp.name)/'staging')
        self.media=observed();self.media['user']={'username':'rco.ucsd'}
        self.post=analyzer_input(self.media,'rco.ucsd')
        self.row=dict(index=1,parent_id=None,account='rco.ucsd',reported_primary_author='rco.ucsd',
            shortcode=self.post['shortcode'],permalink=self.post['permalink'],
            source_object=digest(self.media),analyzer_input_hash=digest(self.post),
            review_revision='a'*64,staged_fingerprint='b'*64,also_discovered_profiles=['rco.ucsd'],
            uncertainty_reasons=['source_verification_unavailable','graphql_errors_unresolved_for_post'],
            source_verification={'source_membership':False,'graphql_integrity':False},
            import_authorized=False,source_authenticity_verified=False)
        self.batch=digest(dict(version=1,purpose='analysis_only_not_import',records=[self.row]))
        atomic(private_dir(self.root/'objects')/(self.row['source_object']+'.json'),self.media)
        atomic(private_dir(self.root/'analysis-batches')/(self.batch+'.json'),dict(batch_id=self.batch,candidates=[self.row]))
        self.found=dict(parent_id=1800,account='rco.ucsd',analyzed=False,has_activities=False,
            has_import_ack=False,caption=self.post['caption'],taken_at=self.post['taken_at'])
        self.patches=[patch.object(binding,'lookup_parents',return_value=[]),
            patch.object(binding,'authoritative_state',return_value={}),
            patch.object(binding.attempt_safety,'inspect',return_value=dict(review_required=False,status='not_recorded')),
            patch('app.parent_pipeline.screening.screen',return_value=dict(status='eligible')),
            patch('app.parent_pipeline.locks.advisory_lock',side_effect=self.lock),
            patch.object(scrape,'insert_analysis_parent',return_value=(1800,True))]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        # Fixtures are deliberately historical; control the cutoff, not production config.
        self.clock=patch.object(binding,'window',return_value=(None,datetime(2026,10,1,tzinfo=timezone.utc)))
        self.clock.start();self.addCleanup(self.clock.stop)
    @staticmethod
    @contextmanager
    def lock(*args):yield True
    def bind(self,**kwargs):return binding.bind_candidate(self.root,self.batch,1,**kwargs)
    def test_dry_run_no_parent_creation_no_local_review_write(self):
        result=self.bind();self.assertEqual(result['status'],'creation_required')
        scrape.insert_analysis_parent.assert_not_called();binding.authoritative_state.assert_not_called()
        self.assertEqual(len(list((self.root/'analysis-batches').glob('*.json'))),1)
    def test_existing_parent_reused_without_writes(self):
        binding.lookup_parents.return_value=[self.found]
        result=self.bind(write=True)
        self.assertEqual(result['parent_id'],1800);self.assertEqual(result['database_writes'],0)
        scrape.insert_analysis_parent.assert_not_called()
    def test_creation_preserves_original_input_and_uncertainty(self):
        binding.lookup_parents.side_effect=[[],[],[self.found]]
        result=self.bind(write=True)
        args=scrape.insert_analysis_parent.call_args.args
        self.assertEqual(args[0],'rco.ucsd');self.assertEqual(args[1],self.post)
        self.assertFalse(args[2]['production_eligible']);self.assertFalse(args[2]['import_authorized'])
        self.assertEqual(args[2]['uncertainty_reasons'],self.row['uncertainty_reasons'])
        record,post=load_input(self.root,result['analysis_batch_id'],1800)
        self.assertEqual(post,self.post);self.assertTrue(record['analysis_authorized'])
        self.assertFalse(record['source_authenticity_verified']);self.assertFalse(record['import_authorized'])
        self.assertEqual(result['paid_requests'],0)
        self.assertEqual((self.root/'analysis-batches'/(result['analysis_batch_id']+'.json')).stat().st_mode&0o077,0)
    def test_repeat_reuses_parent_and_batch_without_new_insert(self):
        binding.lookup_parents.side_effect=[[],[],[self.found],[self.found],[self.found],[self.found]]
        first=self.bind(write=True);second=self.bind(write=True)
        self.assertEqual(first['analysis_batch_id'],second['analysis_batch_id'])
        self.assertEqual(second['database_writes'],0);scrape.insert_analysis_parent.assert_called_once()
    def test_analyzed_imported_conflicting_parent_or_revision_block(self):
        for changes in ({'analyzed':True},{'has_activities':True},{'has_import_ack':True},
                        {'account':'different'},{'caption':'different'},{'taken_at':1}):
            with self.subTest(changes=changes):
                binding.lookup_parents.return_value=[dict(self.found,**changes)]
                with self.assertRaises(binding.Blocked):self.bind(write=True)
        binding.lookup_parents.return_value=[self.found,self.found]
        with self.assertRaisesRegex(binding.Blocked,'canonical_parent_ambiguous'):self.bind(write=True)
        scrape.insert_analysis_parent.assert_not_called()
    def test_failed_journal_is_never_reset_or_retried(self):
        binding.lookup_parents.return_value=[dict(self.found,parent_id=1645)]
        binding.attempt_safety.inspect.return_value=dict(review_required=True,status='review_required')
        with patch.object(binding.attempt_safety,'record') as record:
            with self.assertRaisesRegex(binding.Blocked,'previous_analysis_outcome_requires_review'):self.bind(write=True)
            record.assert_not_called();scrape.insert_analysis_parent.assert_not_called()
    def test_race_rechecked_under_lock(self):
        binding.lookup_parents.side_effect=[[],[dict(self.found,analyzed=True)]]
        with self.assertRaises(binding.Blocked):self.bind(write=True)
        scrape.insert_analysis_parent.assert_not_called()
    def test_failed_creation_does_not_publish_approval(self):
        scrape.insert_analysis_parent.side_effect=RuntimeError('private')
        with self.assertRaises(RuntimeError):self.bind(write=True)
        self.assertEqual(len(list((self.root/'analysis-batches').glob('*.json'))),1)
    def test_source_tampering_and_expiry_rejected(self):
        self.clock.stop()
        with patch.object(binding,'window',return_value=(None,datetime(2100,1,1,tzinfo=timezone.utc))):
            with self.assertRaisesRegex(binding.Blocked,'outside_recent_window'):self.bind()
        (self.root/'objects'/(self.row['source_object']+'.json')).write_text('{}')
        with self.assertRaisesRegex(binding.Blocked,'staged_object_changed'):self.bind()
    def test_cli_requires_all_confirmations_and_one_candidate(self):
        args=['--batch-id',self.batch,'--bind','--candidate','1']
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):cli.main(args)
        args+=['--confirm-parent-write','--approve-analysis-only','--confirm-source-unverified']
        with patch.object(binding,'bind_candidate',return_value={'status':'bound_analysis_only'}) as call,redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args),0);self.assertTrue(call.call_args.kwargs['write'])


class QuarantineTests(unittest.TestCase):
    def test_insert_uses_existing_mapping_and_unique_constraint(self):
        from app.instagram_scraper import scrape_db
        factory=MagicMock();db=factory.return_value.__enter__.return_value
        db.execute.return_value.scalar_one_or_none.return_value=1800
        review=dict(scope='local_analysis_only',production_eligible=False,import_authorized=False)
        post=dict(shortcode='synthetic',permalink='https://www.instagram.com/p/synthetic/',taken_at=1793656800,
            comments=[dict(kind='caption',text='caption')],openable_media_urls={'largest':['https://one.cdninstagram.com/original.jpg'],'other':[]})
        with patch.object(scrape_db,'SessionLocal',factory):
            self.assertEqual(scrape_db.insert_analysis_parent('rco.ucsd',post,review),(1800,True))
        statement=db.execute.call_args.args[0];params=statement.compile().params
        self.assertEqual(params['caption'],'caption');self.assertEqual(params['comments_json']['analysis_review'],review)
        self.assertEqual(params['additional_image_urls'],post['openable_media_urls'])
        self.assertIn('DO NOTHING',str(statement));self.assertNotIn('DO UPDATE',str(statement))
        db.commit.assert_called_once()
    def test_quarantine_is_preserved_during_scrape_refresh(self):
        import test_instagram_scraper_changes as existing
        case=existing.ScrapeChangeTests();post=existing.post();row=case.existing(post)
        row.comments_json['analysis_review']={'production_eligible':False}
        changed=existing.post();changed['comments'][0]['text']='Changed caption'
        _,_,db=case.upsert([changed],row)
        statement=db.execute.call_args.args[0]
        self.assertEqual(statement.compile().params['comments_json']['analysis_review'],row.comments_json['analysis_review'])
    def test_normal_serial_and_threaded_selection_exclude_review_rows(self):
        from app.ai_analyze_service import candidates as serial_candidates
        from app.ai_analyze_service_mutithread import candidates as threaded
        for module in (serial_candidates,threaded):
            for function in ('fetch_instagram_rows_for_profile','distinct_profile_usernames_recent'):
                factory=MagicMock();db=factory.return_value.__enter__.return_value
                db.execute.return_value.scalars.return_value.all.return_value=[]
                db.execute.return_value.all.return_value=[]
                with patch.object(module,'SessionLocal',factory):
                    getattr(module,function)(*(['rco.ucsd'] if function.startswith('fetch') else []))
                self.assertIn('analysis_review',str(db.execute.call_args.args[0].compile().params))
    def test_readonly_lookup_never_flushes_or_commits(self):
        import Blueprint_db
        factory=MagicMock();db=factory.return_value.__enter__.return_value
        db.scalars.return_value.all.return_value=[]
        with patch.object(Blueprint_db,'SessionLocal',factory):self.assertEqual(binding.lookup_parents('synthetic'),[])
        self.assertIn('READ ONLY',str(db.execute.call_args_list[0].args[0]))
        db.commit.assert_not_called();db.add.assert_not_called();db.flush.assert_not_called()

    def test_lookup_reuses_timezone_aware_publication_without_unix_alias(self):
        import Blueprint_db
        factory=MagicMock();db=factory.return_value.__enter__.return_value
        stamp=datetime(2026,11,4,12,tzinfo=timezone.utc)
        db.scalars.return_value.all.return_value=[SimpleNamespace(id=1800,profile_username='rco.ucsd',
            ai_analyzed=False,posted_time=stamp,posted_unix_seconds=None,caption='caption')]
        db.scalar.return_value=None;db.get.return_value=None
        with patch.object(Blueprint_db,'SessionLocal',factory):result=binding.lookup_parents('synthetic')
        self.assertEqual(result[0]['taken_at'],int(stamp.timestamp()))
        db.commit.assert_not_called()

    def test_insert_conflict_reuses_database_id_without_updates(self):
        factory=MagicMock();db=factory.return_value.__enter__.return_value
        db.execute.side_effect=[SimpleNamespace(scalar_one_or_none=lambda:None),SimpleNamespace(scalar_one=lambda:1800)]
        review=dict(scope='local_analysis_only',production_eligible=False,import_authorized=False)
        post=dict(shortcode='synthetic',taken_at=1793656800,comments=[],openable_media_urls={'largest':[]})
        with patch.object(scrape,'SessionLocal',factory):
            self.assertEqual(scrape.insert_analysis_parent('rco.ucsd',post,review),(1800,False))
        for call in db.execute.call_args_list:self.assertNotIn('UPDATE',str(call.args[0]))
