"""Synthetic reviewed contracts/evidence only; no live Instagram schema is claimed."""
import ast
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.instagram_collaboration import AcceptedCollaborationEvidence, normalize_collaboration
from app.instagram_scraper.profile_grid_evidence import GridMembership,NavigationContext
from app.instagram_scraper import permalink_verification as adapter

NOW=datetime(2026,11,4,20,tzinfo=timezone.utc)
CAPTION='Join our workshop November 20, 2026 at 5 PM in CSE B225.'
IMAGE='https://test.cdninstagram.com/private.jpg?sig=SECRET_FIXTURE'
CONTRACT=adapter.ReviewedPermalinkContract('synthetic-fixture-review-not-live','shortcode_media',
    'article.fixture-post','a.fixture-primary-author','.fixture-full-caption','time.fixture-publication',True)


def media(code='OfflineCode',author='dvcatucsd',identity='12345'):
    return dict(code=code,pk=identity,user={'username':author},media_type=1,
                taken_at=int((NOW-timedelta(days=1)).timestamp()),caption={'text':CAPTION},display_url=IMAGE)


def candidate(code='OfflineCode',author='dvcatucsd',identity='12345'):
    return adapter.Candidate('https://www.instagram.com/p/'+code+'/',code,author,identity)


def observation(value=None,*,code='OfflineCode',author='dvcatucsd',identity='12345',accepted=()):
    value=media(code,author,identity) if value is None else value
    proof=adapter.PermalinkPageEvidence(code,author,NOW-timedelta(days=1),CAPTION,NOW,123.0,True,CONTRACT.review_reference)
    return adapter.Observation(candidate(code,author,identity),{'data':{'shortcode_media':value}},proof,True,accepted)


def grid(*codes):
    nav=NavigationContext('dvcatucsd',555.0)
    return GridMembership(nav,NOW,frozenset(codes or ['OfflineCode']),True),nav


class PermalinkVerificationTests(unittest.TestCase):
    def check(self,*items,**kwargs):
        membership,nav=grid(*(o.candidate.shortcode for o in items if isinstance(o,adapter.Observation)))
        options=dict(now=NOW,contract=CONTRACT,membership=membership,profile_navigation=nav)
        options.update(kwargs)
        result=adapter.verify_batch(list(items),**options)
        summary=result['summary']
        self.assertFalse(summary['production_ingestion_enabled'])
        self.assertFalse(summary['profile_timeline_complete'])
        self.assertEqual(summary['database_writes'],0)
        for private in ('OfflineCode','SECRET_FIXTURE','private.jpg','Join our workshop','dvcatucsd'):
            self.assertNotIn(private,json.dumps(summary))
        return result

    def test_valid_single_image(self):
        result=self.check(observation())
        self.assertEqual(result['summary']['eligible_count'],1)
        self.assertEqual(result['verified_posts'][0].image_references,(IMAGE,))
        self.assertEqual(result['summary']['posts'][0]['reasons'],[])
        self.assertNotIn('SECRET_FIXTURE',repr(result['verified_posts']))

    def test_image_only_native_carousel_every_child_verified(self):
        value=media();value.update(media_type=8,carousel_media_count=2,
            carousel_media=[dict(pk='201',media_type=1,display_url=IMAGE),dict(pk='202',media_type=1,display_url=IMAGE)])
        value.pop('display_url')
        result=self.check(observation(value))
        self.assertEqual(result['summary']['eligible_count'],1)
        self.assertEqual(result['summary']['posts'][0]['image_count'],2)

    def test_legacy_permalink_graphql_shape_with_complete_image_sidecar(self):
        value=dict(shortcode='OfflineCode',id='12345',owner={'username':'dvcatucsd'},__typename='GraphSidecar',
                   taken_at_timestamp=int((NOW-timedelta(days=1)).timestamp()),
                   edge_media_to_caption={'edges':[{'node':{'text':CAPTION}}]},
                   edge_sidecar_to_children={'count':2,'page_info':{'has_next_page':False},'edges':[
                       {'node':{'id':'201','__typename':'GraphImage','display_url':IMAGE}},
                       {'node':{'id':'202','__typename':'GraphImage','display_url':IMAGE}}]})
        self.assertEqual(self.check(observation(value))['summary']['eligible_count'],1)

    def test_videos_reels_unknown_and_conflicting_types_rejected(self):
        variants=[dict(media_type=2),dict(__typename='GraphVideo'),dict(is_video=True),dict(video_url='private'),
                  dict(product_type='clips'),dict(media_type=99),dict(media_type=True),dict(__typename='Unknown'),
                  dict(media_type=8,__typename='GraphImage')]
        for update in variants:
            with self.subTest(update=update):
                self.assertEqual(self.check(observation(media()|update))['summary']['eligible_count'],0)
        item=observation()
        reel=replace(item.candidate,permalink='https://www.instagram.com/reel/OfflineCode/')
        self.assertEqual(self.check(replace(item,candidate=reel))['summary']['eligible_count'],0)

    def test_mixed_missing_unknown_and_duplicate_carousel_children_rejected(self):
        for bad in [dict(pk='202',media_type=2,display_url=IMAGE),dict(pk='202',display_url=IMAGE),
                    dict(pk='202',media_type=1),dict(pk='201',media_type=1,display_url=IMAGE),
                    dict(pk='202',media_type=1,is_video=True,display_url=IMAGE)]:
            value=media()|dict(media_type=8,carousel_media_count=2,
                              carousel_media=[dict(pk='201',media_type=1,display_url=IMAGE),bad])
            self.assertEqual(self.check(observation(value))['summary']['eligible_count'],0)

    def test_carousel_missing_count_and_unfinished_pagination_rejected(self):
        value=media()|dict(media_type=8,carousel_media=[dict(pk='201',media_type=1,display_url=IMAGE)])
        self.assertEqual(self.check(observation(value))['summary']['eligible_count'],0)
        value.pop('carousel_media');value['edge_sidecar_to_children']={'count':1,'page_info':{'has_next_page':True},
            'edges':[{'node':dict(pk='201',media_type=1,display_url=IMAGE)}]}
        self.assertEqual(self.check(observation(value))['summary']['eligible_count'],0)

    def test_matching_url_or_author_cannot_supply_independent_page_evidence(self):
        item=observation()
        for proof in [None,{'shortcode':'OfflineCode','primary_author':'dvcatucsd'}]:
            result=self.check(replace(item,page_evidence=proof))
            self.assertEqual(result['summary']['eligible_count'],0)
        wrong=replace(item.page_evidence,primary_author='someone_else')
        self.assertEqual(self.check(replace(item,page_evidence=wrong))['summary']['eligible_count'],0)

    def test_permalink_cannot_supply_profile_membership_even_for_primary_author(self):
        result=self.check(observation(),membership=None,profile_navigation=None)
        decision=result['summary']['posts'][0]
        self.assertTrue(decision['permalink_verified'])
        self.assertFalse(decision['profile_membership_verified'])
        self.assertEqual(result['summary']['eligible_count'],0)
        self.assertIn('profile_membership_unverified',decision['reasons'])

    def test_other_author_requires_distinct_accepted_collaboration_and_membership(self):
        item=observation(author='organizer')
        self.assertEqual(self.check(item)['summary']['eligible_count'],0)
        hinted=copy.deepcopy(item.payload);hinted['data']['shortcode_media']['coauthor_producers']=[{'username':'dvcatucsd','accepted':True}]
        self.assertEqual(self.check(replace(item,payload=hinted))['summary']['eligible_count'],0)
        accepted=(AcceptedCollaborationEvidence('OfflineCode','organizer','dvcatucsd'),)
        item=replace(item,collaboration_evidence=accepted)
        self.assertEqual(self.check(item)['summary']['eligible_count'],1)
        self.assertEqual(self.check(item,membership=None,profile_navigation=None)['summary']['eligible_count'],0)
        wrong=(AcceptedCollaborationEvidence('WrongCode','organizer','dvcatucsd'),)
        self.assertEqual(self.check(replace(item,collaboration_evidence=wrong))['summary']['eligible_count'],0)

    def test_mismatched_identity_author_numeric_ids_and_owner_rejected(self):
        for update in [dict(code='WrongCode'),dict(shortcode='WrongCode'),dict(pk='999'),dict(id='999'),
                       dict(user={'username':'someone_else'}),dict(owner={'username':'someone_else'})]:
            self.assertEqual(self.check(observation(media()|update))['summary']['eligible_count'],0)

    def test_conflicting_duplicate_observations_quarantine_valid_peer(self):
        first=observation();second=observation(media()|dict(user={'username':'someone_else'}))
        result=self.check(first,second)
        self.assertEqual(result['summary']['eligible_count'],0)
        self.assertIn('conflicting_or_incomplete_observations',result['summary']['posts'][0]['reasons'])

    def test_partial_posts_errors_and_ambiguous_roots_rejected_without_batch_failure(self):
        for field in ['pk','taken_at','caption','user','display_url','media_type']:
            value=media();value.pop(field)
            result=self.check(observation(value),observation(code='OtherCode',identity='999'))
            self.assertEqual(result['summary']['eligible_count'],1)
            self.assertNotEqual(result['summary']['posts'][0]['status'],'eligible_offline')
        item=observation()
        for payload in [{'data':{'shortcode_media':media()},'errors':[{'message':'SECRET_FIXTURE'}]},
                        {'data':{'shortcode_media':media(),'xdt_shortcode_media':media()}},
                        {'data':{'unreviewed':media()}}]:
            self.assertEqual(self.check(replace(item,payload=payload))['summary']['eligible_count'],0)

    def test_stale_unauthed_grid_wrong_context_and_stale_page_rejected(self):
        membership,nav=grid()
        for changed in [replace(membership,observed_at=NOW-timedelta(minutes=6)),
                        replace(membership,authenticated=False),replace(membership,shortcodes=frozenset()),
                        replace(membership,context=NavigationContext('another',555.0))]:
            self.assertEqual(self.check(observation(),membership=changed,profile_navigation=nav)['summary']['eligible_count'],0)
        item=observation()
        for proof in [replace(item.page_evidence,observed_at=NOW-timedelta(minutes=6)),
                      replace(item.page_evidence,published_at=NOW),replace(item.page_evidence,caption='Truncated')]:
            self.assertEqual(self.check(replace(item,page_evidence=proof))['summary']['eligible_count'],0)
        self.assertEqual(self.check(replace(item,authenticated=False))['summary']['eligible_count'],0)

    def test_existing_policy_freshness_and_event_screening_still_apply(self):
        item=observation();value=media();value['taken_at']=int((NOW-timedelta(days=41)).timestamp())
        proof=replace(item.page_evidence,published_at=NOW-timedelta(days=41))
        self.assertEqual(self.check(replace(item,payload={'data':{'shortcode_media':value}},page_evidence=proof))['summary']['eligible_count'],0)
        existing=normalize_collaboration(media(),'dvcatucsd')
        self.assertEqual(self.check(item,existing_posts=(existing,))['summary']['eligible_count'],0)

    def test_loaded_page_capture_requires_reviewed_independent_dom_and_stable_identity(self):
        item=observation();ctx={'url':item.candidate.permalink,'epoch':123.0}
        snapshot=ctx|dict(scoped=True,canonical_url=item.candidate.permalink,author_url='https://www.instagram.com/dvcatucsd/',
                          published_at=(NOW-timedelta(days=1)).isoformat(),caption=CAPTION)
        page=Mock();page.evaluate.side_effect=[ctx,snapshot,ctx]
        proof=adapter.capture_loaded_permalink(page,item.candidate,now=NOW,contract=CONTRACT,authenticated=True)
        self.assertIsInstance(proof,adapter.PermalinkPageEvidence)
        page.goto.assert_not_called();page.click.assert_not_called()
        page=Mock()
        self.assertIsNone(adapter.capture_loaded_permalink(page,item.candidate,now=NOW,authenticated=True))
        page.evaluate.assert_not_called()
        for changed in [snapshot|dict(canonical_url='https://www.instagram.com/p/WrongCode/'),
                        snapshot|dict(author_url='https://www.instagram.com/someone_else/'),
                        snapshot|dict(scoped=False),snapshot|dict(caption=None)]:
            page=Mock();page.evaluate.side_effect=[ctx,changed,ctx]
            self.assertIsNone(adapter.capture_loaded_permalink(page,item.candidate,now=NOW,contract=CONTRACT,authenticated=True))

    def test_verified_handoff_is_detached_from_mutable_input(self):
        value=media()|dict(media_type=8,carousel_media_count=1,
                          carousel_media=[dict(pk='201',media_type=1,display_url=IMAGE)])
        item=observation(value)
        result=self.check(item)
        value['carousel_media'][0]['media_type']=2
        self.assertEqual(result['verified_posts'][0].media['carousel_media'][0]['media_type'],1)

    def test_reel_prefix_query_credentials_and_encoded_paths_do_not_prove_identity(self):
        item=observation()
        for url in ['https://www.instagram.com/dvcatucsd/p/OfflineCode/',
                    'https://www.instagram.com/p/OfflineCode/?author=dvcatucsd',
                    'https://user:secret@www.instagram.com/p/OfflineCode/',
                    'https://www.instagram.com/p/%4FfflineCode/',
                    'https://www.instagram.com.evil.example/p/OfflineCode/']:
            with self.subTest(url=url):
                result=self.check(replace(item,candidate=replace(item.candidate,permalink=url)))
                self.assertEqual(result['summary']['eligible_count'],0)

    def test_carousel_cap_incomplete_refs_and_identity_collisions(self):
        value=media()|dict(media_type=8,carousel_media_count=21,
            carousel_media=[dict(pk=str(200+i),media_type=1,display_url=IMAGE) for i in range(21)])
        self.assertEqual(self.check(observation(value))['summary']['eligible_count'],0)
        value=media()|dict(media_type=8,carousel_media_count=1,
                          carousel_media=[dict(pk='12345',media_type=1,display_url=IMAGE)])
        self.assertEqual(self.check(observation(value))['summary']['eligible_count'],0)
        value=media();value['display_url']='https://untrusted.example/x.jpg'
        self.assertEqual(self.check(observation(value))['summary']['eligible_count'],0)

    def test_bounds_missing_contract_and_malformed_inputs(self):
        self.assertEqual(self.check(observation(),contract=None)['summary']['eligible_count'],0)
        self.assertEqual(self.check(None)['summary']['eligible_count'],0)
        with self.assertRaises(ValueError):adapter.verify_batch([observation()]*26,now=NOW,contract=CONTRACT)
        item=observation();payload={'data':{'shortcode_media':media()},'huge':'x'*20001}
        self.assertEqual(self.check(replace(item,payload=payload))['summary']['eligible_count'],0)
        page=Mock();page.evaluate.side_effect=RuntimeError('SECRET_FIXTURE')
        self.assertIsNone(adapter.capture_loaded_permalink(page,item.candidate,now=NOW,contract=CONTRACT,authenticated=True))

    def test_no_network_browser_launch_database_or_production_activation(self):
        tree=ast.parse(Path(adapter.__file__).read_text())
        imports=[node.module or '' for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
        self.assertFalse(any(any(name in value for name in ['Blueprint_db','playwright','sqlalchemy','httpx']) for value in imports))
        calls=[node.func.attr for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)]
        for forbidden in ['goto','launch','commit','connect','post','get','put_object']:
            if forbidden=='get':continue  # dict.get is intentionally used for fixed field access.
            self.assertNotIn(forbidden,calls)
