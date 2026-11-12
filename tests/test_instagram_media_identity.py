"""Identity semantics only; no real capture values or new source attestations."""
import copy
import json
import unittest

from app.instagram_scraper.media_identity import inspect_identity, normalize_identity, SHAPES, RELATIONS
from app.instagram_scraper.permalink_verification import _normalize, Unverified
from app.instagram_scraper.daily_pilot import GraphQLGate
from test_instagram_permalink_verification import media, NOW
from test_instagram_daily_pilot import data


class MediaIdentityTests(unittest.TestCase):
    def reason(self,value):
        with self.assertRaises(ValueError) as caught:normalize_identity(value)
        return str(caught.exception)

    def test_code_is_shortcode_not_numeric_media_id(self):
        value=media();value.update(pk='12345',id='12345')
        normalized,_=_normalize(value)
        self.assertEqual(normalized['code'],'OfflineCode')
        self.assertEqual(normalized['pk'],'12345')
        self.assertEqual(self.reason({'code':'OfflineCode'}),'missing_media_identity')
        self.assertEqual(self.reason({'pk':'OfflineCode'}),'unsupported_media_identity_layout')

    def test_observed_flat_field_layout_with_matching_numeric_aliases(self):
        value=media();value.update(pk=12345,id='12345',image_versions2={
            'candidates':[{'url':'https://test.cdninstagram.com/private.jpg'}]})
        value.pop('display_url')
        normalized,images=_normalize(value)
        self.assertEqual(normalized['pk'],'12345');self.assertEqual(len(images),1)

    def test_numeric_representation_differences_are_not_identity_conflicts(self):
        for pk,node in ((12345,'12345'),('00012345',12345),(12345,'00012345'),('00012345','0012345')):
            self.assertEqual(normalize_identity({'pk':pk,'id':node}),'12345')

    def test_legacy_pk_only_and_numeric_id_only_remain_supported(self):
        self.assertEqual(normalize_identity({'pk':'12345'}),'12345')
        self.assertEqual(normalize_identity({'id':12345}),'12345')

    def test_distinct_numeric_aliases_are_real_conflicts(self):
        self.assertEqual(self.reason({'pk':'12345','id':'99999'}),'conflicting_media_identity')

    def test_missing_null_and_empty_fields_are_distinct(self):
        self.assertEqual(self.reason({}),'missing_media_identity')
        for fields in ({'pk':None},{'pk':'12345','id':None},{'pk':'12345','id':''}):
            self.assertEqual(self.reason(fields),'incomplete_media_identity')

    def test_compound_layout_does_not_get_split_or_treated_as_confirmed_alias(self):
        for value in ('12345_67890','99999_67890'):
            fields={'pk':'12345','id':value}
            self.assertEqual(inspect_identity(fields)['id_shape'],'compound_numeric')
            self.assertEqual(self.reason(fields),'unsupported_media_identity_layout')
        self.assertEqual(self.reason({'id':'12345_67890'}),'unsupported_media_identity_layout')

    def test_opaque_ids_remain_unsupported_even_with_valid_pk(self):
        self.assertEqual(self.reason({'pk':'12345','id':'opaque:SECRET_ID'}),'unsupported_media_identity_layout')

    def test_bad_numeric_values_or_types_do_not_fall_back_to_valid_peer(self):
        for value in (True,0,-1,1.5,{},[], '0','-1','1.0','9'*33):
            self.assertEqual(self.reason({'pk':'12345','id':value}),'invalid_media_identity')

    def test_shortcodes_stay_case_sensitive_and_conflicts_still_reject(self):
        value=media();value.update(shortcode='offlinecode')
        with self.assertRaisesRegex(Unverified,'conflicting_post_identity'):_normalize(value)

    def test_carousel_children_share_canonical_identity_and_duplicate_detection(self):
        value=media();value.update(media_type=8,carousel_media_count=2,carousel_media=[
            dict(pk='201',id='000201',media_type=1,display_url='https://test.cdninstagram.com/a.jpg'),
            dict(pk=202,id='202',media_type=1,display_url='https://test.cdninstagram.com/b.jpg')])
        normalized,images=_normalize(value)
        self.assertEqual(len(images),2)
        value['carousel_media'][1].update(pk='00201',id=201)
        with self.assertRaisesRegex(Unverified,'duplicate_carousel_child'):_normalize(value)

    def test_carousel_child_parent_collision_is_rejected_after_numeric_normalization(self):
        value=media();value.update(media_type=8,carousel_media_count=1,carousel_media=[
            dict(pk='00012345',media_type=1,display_url='https://test.cdninstagram.com/a.jpg')])
        with self.assertRaisesRegex(Unverified,'duplicate_carousel_child'):_normalize(value)

    def test_carousel_missing_conflicting_or_unsupported_child_identity(self):
        value=media();value.update(media_type=8,carousel_media_count=1)
        for fields,reason in (({},'missing_media_identity'),({'pk':'201','id':'202'},'conflicting_media_identity'),
                              ({'pk':'201','id':'201_999'},'unsupported_media_identity_layout')):
            value['carousel_media']=[dict(fields,media_type=1,display_url='https://test.cdninstagram.com/a.jpg')]
            with self.assertRaisesRegex(Unverified,reason):_normalize(value)

    def test_identity_pass_does_not_bypass_video_or_source_or_graphql_integrity(self):
        value=media();value.update(pk=12345,id='00012345')
        payload=data(value,errors=[{'path':['profile_grid','edges',0,'node','SECRET_FIELD']}])
        gate=GraphQLGate(now=NOW);gate.observe(payload,'graphql_error')
        result=gate.summary()
        self.assertEqual(result['image_fields_valid'],1)
        self.assertEqual(result['accepted'],0)
        self.assertEqual(result['reason_counts']['source_verification_unavailable'],1)
        self.assertEqual(result['reason_counts']['graphql_errors_unresolved_for_post'],1)
        value['media_type']=2
        with self.assertRaisesRegex(Unverified,'video_or_unknown_media'):_normalize(value)

    def test_daily_reason_codes_distinguish_actual_conflict_and_unsupported_format(self):
        for fields,reason in (({'pk':12345,'id':'12345_999'},'unsupported_media_identity_layout'),
                              ({'pk':12345,'id':'999'},'conflicting_media_identity')):
            gate=GraphQLGate(now=NOW);gate.observe(data(media()|fields),'graphql_error')
            result=gate.summary()
            self.assertEqual(result['reason_counts'][reason],1)
            self.assertNotIn('conflicting_or_missing_media_identity',result['reason_counts'])

    def test_diagnostics_expose_only_fixed_shapes_and_relations(self):
        value={'pk':'12345','id':'opaque:SECRET_ID'};before=copy.deepcopy(value)
        info=inspect_identity(value)
        self.assertIn(info['pk_shape'],SHAPES);self.assertIn(info['id_shape'],SHAPES)
        self.assertIn(info['relation'],RELATIONS)
        for private in ('12345','SECRET'):self.assertNotIn(private,json.dumps(info))
        self.assertEqual(value,before)
