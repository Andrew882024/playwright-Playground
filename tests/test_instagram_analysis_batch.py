"""Preparation only: synthetic metadata, no API dispatch or production writes."""
from datetime import datetime,timezone,timedelta
from pathlib import Path
import contextlib,copy,io,json,tempfile,unittest
from unittest.mock import patch,Mock

from app.instagram_scraper.analysis_batch import prepare_batch,analyzer_input
from app.instagram_scraper.collection_staging import StagingCollector,atomic
from app.instagram_event_master_projection import parent_marker
import prepare_instagram_analysis_batch as cli
from test_instagram_daily_images import observed,payload

NOW=datetime(2026,11,5,20,tzinfo=timezone.utc)
BUDGET=dict(status='available',providers={'gemini':dict(limit=1,used=0,remaining=1)})


class AnalysisBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)/'stage';self.n=0
    def add(self,profile,index,**changes):
        value=observed(index);value['user']={'username':profile};value.update(changes)
        self.n+=1;c=StagingCollector(self.root,str(self.n),'initial',NOW,profile=profile)
        c.observe(payload(value),'graphql_error');return value
    def prepare(self,**kwargs):
        return prepare_batch(self.root,now=NOW,journal_inspector=lambda p:dict(review_required=False,status='not_recorded'),budget_snapshot=BUDGET,**kwargs)
    def test_unique_posts_diversity_and_recent_order(self):
        self.add('rco.ucsd',1);self.add('rco.ucsd',2,taken_at=int((NOW-timedelta(days=1)).timestamp()))
        value=self.add('tritongamingsd',3,taken_at=int((NOW-timedelta(hours=1)).timestamp()))
        self.add('vgdc.at.ucsd',3,user={'username':'tritongamingsd'},taken_at=value['taken_at'])
        self.add('envisionucsd',4,taken_at=int((NOW-timedelta(days=2)).timestamp()))
        result=self.prepare();self.assertEqual(result['selected_count'],4)
        self.assertEqual(result['duplicate_observations_removed'],1)
        self.assertEqual([r['account'] for r in result['candidates'][:3]],['rco.ucsd','tritongamingsd','envisionucsd'])
        triton=next(r for r in result['candidates'] if r['account']=='tritongamingsd')
        self.assertEqual(triton['also_discovered_profiles'],['tritongamingsd','vgdc.at.ucsd'])
        self.assertEqual(len({r['shortcode'] for r in result['candidates']}),4)
    def test_imported_analyzed_expired_media_and_missing_dates_are_excluded(self):
        self.add('rco.ucsd',1)
        atomic(self.root/'canonical_imports.json',dict(version=1,imports=[dict(shortcode='synthetic1',parent_id=1,event_id=16,marker=parent_marker(1))],processed=[dict(shortcode='synthetic2',parent_id=2,analyzed=True)]))
        self.add('rco.ucsd',2);self.add('rco.ucsd',3,caption={'text':'Workshop October 20, 2026 at 6 PM. This is a synthetic caption.'})
        self.add('rco.ucsd',4,media_type=2)
        self.add('rco.ucsd',5,caption={'text':'A workshop is coming soon. Details will be available later.'})
        self.assertEqual(self.prepare()['selected_count'],0)
    def test_unknown_publication_is_not_selected(self):
        m=self.add('rco.ucsd',1);m.pop('taken_at');m['code']='another'
        c=StagingCollector(self.root,'extra','initial',NOW);c.observe(payload(m),'graphql_error')
        result=self.prepare();self.assertEqual(result['selected_count'],1)
        self.assertIn('publication_not_recent_and_reliable',result['exclusion_counts'])
    def test_existing_parent_binding_journal_and_import_permission_remain_separate(self):
        self.add('envisionucsd',1)
        atomic(self.root/'canonical_imports.json',dict(version=1,imports=[],processed=[dict(shortcode='synthetic1',account='envisionucsd',parent_id=1645,analyzed=False)]))
        result=self.prepare();row=result['candidates'][0]
        self.assertEqual(row['parent_id'],1645);self.assertEqual(result['recommended_first_candidate'],1)
        self.assertFalse(row['analysis_authorized']);self.assertFalse(row['import_authorized']);self.assertFalse(row['source_authenticity_verified'])
        self.assertIn('unsupported_media_identity_layout',row['uncertainty_reasons'])
        self.assertEqual(result['estimated_total_gemini_http_requests'],1)
        self.assertEqual(result['actual_gemini_requests'],0)
    def test_prior_uncertain_attempt_is_not_new_analysis_work(self):
        self.add('envisionucsd',1)
        atomic(self.root/'canonical_imports.json',dict(version=1,imports=[],processed=[dict(shortcode='synthetic1',parent_id=1645,analyzed=False)]))
        result=prepare_batch(self.root,now=NOW,journal_inspector=lambda p:dict(review_required=True),budget_snapshot=BUDGET)
        self.assertEqual(result['selected_count'],0)
        self.assertEqual(result['exclusion_counts']['prior_paid_attempt_requires_separate_retry_approval'],1)
    def test_input_uses_original_first_image_caption_and_preserves_origin_uncertainty(self):
        value=observed();before=copy.deepcopy(value);post=analyzer_input(value,'dvcatucsd')
        self.assertEqual(value,before);self.assertEqual(post['first_image_url'],post['openable_media_urls']['largest'][0])
        self.assertEqual(len(post['openable_media_urls']['largest']),1)
        self.assertEqual(post['caption'],value['caption']['text']);self.assertFalse(post['source_authenticity_verified'])
    def test_limit_and_staged_integrity_guard(self):
        with self.assertRaises(ValueError):self.prepare(limit=6)
        self.add('rco.ucsd',1)
        state=json.loads((self.root/'state.json').read_text());r=next(iter(state['records'].values()))
        p=self.root/'objects'/(r['latest_object']+'.json');data=json.loads(p.read_text());data['caption']['text']='Changed'
        p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):self.prepare()
    def test_preparation_never_dispatches_or_logs_raw_caption_image_url(self):
        self.add('rco.ucsd',1)
        with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            result=self.prepare()
        output=json.dumps(result)
        self.assertNotIn('cdninstagram',output);self.assertNotIn('Workshop November',output)
        self.assertFalse(result['covers_authorized']);self.assertFalse(result['imports_authorized'])
    def test_cli_can_only_prepare_or_save_private_manifest(self):
        self.add('rco.ucsd',1);prepared=self.prepare()
        with patch.object(cli,'STAGING',self.root),patch.object(cli,'prepare_batch',return_value=prepared),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--save']),0)
        saved=next((self.root/'analysis-batches').glob('*.json'))
        self.assertEqual(saved.stat().st_mode & 0o077,0)
        self.assertFalse(json.loads(saved.read_text())['imports_authorized'])
