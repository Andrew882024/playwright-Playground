"""Offline image recovery, not origin/identity/GraphQL acceptance tests."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.instagram_scraper.original_image_adapter import recover_original_images,ImageRecoveryError
from app.instagram_scraper.permalink_verification import _kind,_normalize,Unverified
from app.instagram_scraper import sync_ins


def single(asset='original.jpg'):
    # Public schema SHAPE from staged NITRO, with synthetic IDs/URLs/text.
    return dict(__typename='XDTMediaDict',media_type=1,product_type='feed',
        video_versions=None,video_dash_manifest=None,clips_metadata=None,
        carousel_media=None,carousel_media_count=None,
        image_versions2={'candidates':[
            {'url':'https://test.cdninstagram.com/'+asset+'?size=small','width':100,'height':200},
            {'url':'https://test.cdninstagram.com/'+asset+'?size=large','width':1088,'height':1440}]},
        code='synthetic',pk='123',id='123_456',user={'username':'synthetic'},
        caption={'text':'Synthetic caption'},taken_at=1793666400)


class OriginalImageAdapterTests(unittest.TestCase):
    def test_observed_nullable_schema_recovers_original_without_mutation_or_production_acceptance(self):
        value=single();before=copy.deepcopy(value)
        result=recover_original_images(value)
        self.assertEqual(result.first_image_url,value['image_versions2']['candidates'][1]['url'])
        self.assertEqual(result.variant_count,2);self.assertEqual(result.layout,'single_image')
        self.assertEqual(value,before);self.assertFalse(result.production_eligible)
        with self.assertRaises(Unverified):_kind(value)
        with self.assertRaises(Unverified):_normalize(value)
        projection=result.inspection()
        for key in ('source_membership_verified','media_identity_verified','graphql_integrity_verified','reference_bytes_fetched'):
            self.assertFalse(projection[key])
        self.assertNotIn('cdninstagram',json.dumps(projection));self.assertNotIn('?size',repr(result))
    def test_all_seven_observed_variant_counts(self):
        audit=json.loads((Path(__file__).parent/'fixtures/nitro_original_image_audit.json').read_text())
        for n in audit['staged_reference_variant_counts']:
            value=single();ref=value['image_versions2']['candidates'][0]
            value['image_versions2']['candidates']=[dict(ref,width=100+i,height=200+i) for i in range(n)]
            result=recover_original_images(value)
            self.assertEqual(len(result.image_urls),1);self.assertEqual(result.variant_count,n)
    def test_uses_existing_post_only_extractor_without_avatar_fallback(self):
        value=single();value['user']['profile_pic_url']='https://test.cdninstagram.com/avatar.jpg'
        with patch.object(sync_ins,'_post_media_cdn_urls_from_timeline_node',wraps=sync_ins._post_media_cdn_urls_from_timeline_node) as extract:
            result=recover_original_images(value)
        extract.assert_called_once();self.assertNotIn('user',extract.call_args.args[0])
        self.assertNotIn('avatar',result.first_image_url)
    def test_legacy_graphimage_supported(self):
        value=single();value['__typename']='GraphImage'
        for key in ('video_versions','video_dash_manifest','clips_metadata','carousel_media','carousel_media_count'):value.pop(key)
        self.assertEqual(recover_original_images(value).layout,'single_image')
    def test_videos_reels_nonnull_video_fields_and_unknown_types_rejected(self):
        changes=[{'media_type':2},{'media_type':True},{'media_type':None},{'__typename':'Unknown'},
                 {'__typename':'GraphVideo'},{'product_type':'clips'},{'is_video':True},{'is_video':None}]
        changes += [{key:value} for key in ('video_versions','video_dash_manifest','clips_metadata','video_url')
                    for value in ([],{},'https://test.cdninstagram.com/video.mp4')]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(ImageRecoveryError):
                recover_original_images(dict(single(),**change))
    def test_missing_images_not_rescued_by_generated_cover_or_avatar(self):
        value=single();value.pop('image_versions2')
        value.update(generated_image_url='https://example.invalid/cover.png')
        value['user']['profile_pic_url']='https://test.cdninstagram.com/avatar.jpg'
        with self.assertRaisesRegex(ImageRecoveryError,'missing_original_image_reference'):recover_original_images(value)
    def test_multiple_asset_groups_in_single_image_are_rejected(self):
        value=single();value['image_versions2']['candidates'][1]['url']='https://test.cdninstagram.com/different.jpg'
        with self.assertRaisesRegex(ImageRecoveryError,'conflicting_image_assets'):recover_original_images(value)
    def test_malformed_candidates_dimensions_and_unsafe_urls_rejected(self):
        candidates=[None,[],{},[None],[{'url':'https://test.cdninstagram.com/a.jpg','width':True,'height':10}],
                    [{'url':'https://test.cdninstagram.com/a.jpg','width':10}],
                    [{'url':'https://test.cdninstagram.com@evil.invalid/a.jpg'}],
                    [{'url':'https://evil.invalid/cdninstagram.com/a.jpg'}],
                    [{'url':'https://test.cdninstagram.com/a.mp4'}],
                    [{'url':'https://test.cdninstagram.com:444/a.jpg'}]]
        for items in candidates:
            value=single();value['image_versions2']['candidates']=items
            with self.subTest(items=items),self.assertRaises(ImageRecoveryError):recover_original_images(value)
    def carousel(self):
        value=single();value.update(media_type=8,carousel_media_count=2,carousel_media=[single('z-first.jpg'),single('a-second.jpg')])
        return value
    def test_carousel_original_order_not_alphabetical_url_order(self):
        result=recover_original_images(self.carousel())
        self.assertIn('z-first',result.image_urls[0]);self.assertIn('a-second',result.image_urls[1])
        self.assertEqual(result.original_order_basis,'explicit_carousel_child_array')
    def test_incomplete_mixed_duplicate_or_unknown_carousel_children_rejected(self):
        changes=[]
        value=self.carousel();value['carousel_media_count']=3;changes.append(value)
        value=self.carousel();value['carousel_media'][1]['media_type']=2;changes.append(value)
        value=self.carousel();value['carousel_media'][1]=None;changes.append(value)
        value=self.carousel();value['carousel_media'][1]=copy.deepcopy(value['carousel_media'][0]);changes.append(value)
        value=self.carousel();value['carousel_media'][1].pop('media_type');changes.append(value)
        for value in changes:
            with self.assertRaises(ImageRecoveryError):recover_original_images(value)
    def test_no_browser_database_download_or_cover_operation(self):
        with patch.object(sync_ins,'sync_playwright',side_effect=AssertionError('browser')), \
             patch.object(sync_ins,'upsert_posts_for_profile',side_effect=AssertionError('database')), \
             patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            self.assertEqual(recover_original_images(single()).layout,'single_image')
    def test_nitro_audit_is_original_source_not_generated_cover(self):
        audit=json.loads((Path(__file__).parent/'fixtures/nitro_original_image_audit.json').read_text())
        self.assertEqual((audit['parent_id'],audit['event_master_id']),(1669,16))
        self.assertTrue(audit['stored_main_same_asset_as_staged_variants'])
        self.assertEqual(audit['cached_original_dimensions'],[1088,1440])
        self.assertFalse(audit['historical_gemini_exact_request_payload_retained'])

    def test_arbitrary_nested_urls_do_not_enter_legacy_projection(self):
        value=single();value['image_versions2']['unrelated']={'url':'https://test.cdninstagram.com/avatar.jpg'}
        result=recover_original_images(value)
        self.assertIn('original.jpg',result.first_image_url)
        self.assertNotIn('avatar',result.first_image_url)
