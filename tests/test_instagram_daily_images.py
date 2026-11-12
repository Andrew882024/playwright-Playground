"""Daily image integration with public structural shapes, synthetic private values."""
import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.instagram_scraper import daily_pilot as pilot
from app.instagram_scraper.permalink_verification import _normalize,Unverified
from app.instagram_scraper.post_ingestion_policy import inspect_non_image_requirements,PostObservation,evaluate_posts
from app.ai_analyze_service.event_evidence import prioritize_images
from test_instagram_original_image_adapter import single

NOW=datetime(2026,11,5,20,tzinfo=timezone.utc)
ROOT='xdt_api__v1__feed__user_timeline_graphql_connection'


def observed(index=0,count=13):
    media=single('synthetic'+str(index)+'.jpg')
    media.update(code='synthetic'+str(index),user={'username':'dvcatucsd'},
                 taken_at=int(NOW.timestamp()),caption={'text':'Workshop November 20, 2026 at 6 PM in Room 1.'})
    ref=media['image_versions2']['candidates'][0]
    media['image_versions2']['candidates']=[dict(ref,width=100+i,height=200+i) for i in range(count)]
    return media


def payload(*media,errored=True):
    value={'data':{ROOT:{'edges':[{'node':item} for item in media]}}}
    if errored:value['errors']=[{'path':[ROOT,'edges',0,'node','SYNTHETIC_UNKNOWN']}]
    return value


class DailyImageTests(unittest.TestCase):
    def test_seven_structural_shapes_pass_images_but_no_other_verification(self):
        audit=json.loads((Path(__file__).parent/'fixtures/nitro_original_image_audit.json').read_text())
        media=[observed(i,count) for i,count in enumerate(audit['staged_reference_variant_counts'])]
        before=copy.deepcopy(media);gate=pilot.GraphQLGate(now=NOW)
        gate.observe(payload(*media),'graphql_error');result=gate.summary()
        self.assertEqual(media,before)
        self.assertEqual(result['image_only_validation'],dict(passed=7,failed=0,not_inspected=0,single_image=7,
            image_only_carousel=0,original_images=7,resolution_variants=94,reason_counts={}))
        self.assertEqual(result['accepted'],0);self.assertEqual(result['skipped'],7)
        for reason in ('unsupported_media_identity_layout','source_verification_unavailable',
                       'graphql_errors_unresolved_for_post','production_handoff_disabled','authentication_unverified'):
            self.assertEqual(result['reason_counts'][reason],7)
        self.assertNotIn('incomplete_carousel',result['reason_counts'])
        self.assertNotIn('unknown_media_type',result['reason_counts'])
        self.assertNotIn('cdninstagram',json.dumps(result))
    def test_numeric_identity_does_not_turn_image_success_into_source_proof(self):
        media=observed();media['id']='123'
        gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(media,errored=False),'valid_timeline')
        result=gate.summary();self.assertEqual(result['image_fields_valid'],1)
        self.assertNotIn('unsupported_media_identity_layout',result['reason_counts'])
        self.assertIn('source_verification_unavailable',result['reason_counts'])
        self.assertEqual(result['accepted'],0)
        with self.assertRaises(Unverified):_normalize(media)  # legacy full policy unchanged
    def test_carousel_preserves_first_slide_not_resolution_variant_order(self):
        first=observed();first['id']='123';first['pk']='123'
        second=observed(1);second['id']='124';second['pk']='124'
        first['image_versions2']['candidates'][0]['url']='https://test.cdninstagram.com/z-first.jpg'
        first['image_versions2']['candidates']=first['image_versions2']['candidates'][:1]
        second['image_versions2']['candidates'][0]['url']='https://test.cdninstagram.com/a-second.jpg'
        second['image_versions2']['candidates']=second['image_versions2']['candidates'][:1]
        media=observed();media.update(id='125',pk='125',media_type=8,carousel_media_count=2,carousel_media=[first,second])
        with patch.object(pilot,'recover_original_images',wraps=pilot.recover_original_images) as adapter:
            gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(media),'graphql_error')
        adapter.assert_called_once_with(media)
        refs=pilot.recover_original_images(media)
        self.assertIn('z-first',refs.first_image_url)
        # Future trusted handoff must keep precisely this order; this test creates
        # no production source attestation and invokes no Gemini client.
        self.assertEqual(prioritize_images(list(refs.image_urls),{}),list(refs.image_urls))
        self.assertEqual(gate.summary()['image_only_validation']['image_only_carousel'],1)
    def test_video_reel_mixed_missing_unknown_and_ambiguous_assets_rejected(self):
        changes=[]
        for patch_value in ({'media_type':2},{'product_type':'clips'},{'media_type':None},
                            {'video_versions':[]},{'image_versions2':{'candidates':[]}}):
            changes.append(dict(observed(),**patch_value))
        mixed=observed();mixed.update(media_type=8,carousel_media_count=2,
            carousel_media=[observed(1),dict(observed(2),media_type=2)]);changes.append(mixed)
        incomplete=observed();incomplete.update(media_type=8,carousel_media_count=2,carousel_media=[observed(1)]);changes.append(incomplete)
        ambiguous=observed();ambiguous['image_versions2']['candidates'][1]['url']='https://test.cdninstagram.com/another.jpg';changes.append(ambiguous)
        gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(*changes),'graphql_error')
        result=gate.summary();self.assertEqual(result['image_only_validation']['passed'],0)
        self.assertEqual(result['image_only_validation']['failed']+result['image_only_validation']['not_inspected'],len(changes));self.assertEqual(result['accepted'],0)
    def test_child_identity_conflict_not_masked_by_recovered_images(self):
        children=[dict(observed(i),id='123',pk='123') for i in range(2)]
        media=observed();media.update(id='125',pk='125',media_type=8,carousel_media_count=2,carousel_media=children)
        gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(media),'graphql_error')
        self.assertEqual(gate.summary()['image_fields_valid'],1)
        self.assertEqual(gate.summary()['reason_counts']['duplicate_carousel_child'],1)
    def test_freshness_expiry_and_review_still_apply_when_compound_identity_blocks(self):
        media=observed();media['taken_at']=int(NOW.timestamp())-41*86400
        media['caption']={'text':'Workshop October 20, 2026 at 6 PM.'}
        review=observed(1);review['caption']={'text':'A workshop is coming soon.'}
        gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(media,review),'graphql_error')
        result=gate.summary();self.assertEqual(result['image_fields_valid'],2)
        for reason in ('post_outside_freshness_window','event_expired','event_screening_requires_review'):
            self.assertIn(reason,result['reason_counts'])
    def test_unverified_coauthor_remains_blocked(self):
        media=observed();media['user']={'username':'another'};media['coauthor_producers']=[{'username':'dvcatucsd'}]
        gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(media),'graphql_error')
        result=gate.summary();self.assertEqual(result['image_fields_valid'],1)
        self.assertIn('accepted_collaboration_unverified',result['reason_counts'])
        self.assertEqual(result['accepted'],0)
    def test_inspection_cannot_return_eligible_or_change_default_policy(self):
        media=observed();observation=PostObservation({'node':media},'dvcatucsd')
        decision=inspect_non_image_requirements(observation,now=NOW)
        self.assertEqual(decision['status'],'inspection_only')
        self.assertIn('production_handoff_disabled',decision['reasons'])
        normal=evaluate_posts([observation],now=NOW)['posts'][0]
        self.assertIn('incomplete_carousel',normal['reasons'])  # original policy unchanged
    def test_unexpected_image_exception_is_sanitized(self):
        with patch.object(pilot,'recover_original_images',side_effect=RuntimeError('SECRET https://private.invalid/key')):
            gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(observed()),'graphql_error')
        result=gate.summary();self.assertEqual(result['image_only_validation']['failed'],1)
        self.assertNotIn('SECRET',json.dumps(result));self.assertNotIn('private.invalid',json.dumps(result))
    def test_integration_does_not_call_browser_or_database(self):
        with patch('app.instagram_scraper.sync_ins.sync_playwright',side_effect=AssertionError('browser')), \
             patch('app.instagram_scraper.sync_ins.upsert_posts_for_profile',side_effect=AssertionError('database')), \
             patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            gate=pilot.GraphQLGate(now=NOW);gate.observe(payload(observed()),'graphql_error')
            self.assertEqual(gate.summary()['image_fields_valid'],1)
