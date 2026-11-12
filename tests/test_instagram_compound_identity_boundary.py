"""Compound syntax is observed; component semantics are not yet established.

These synthetic vectors are rejection tests, NOT trustworthy Instagram fixtures
or attestations about the meaning of either numeric component.
"""
import copy
import json
import unittest

from app.instagram_scraper.media_identity import inspect_identity, normalize_identity
from app.instagram_scraper.permalink_verification import _normalize, Unverified
from app.instagram_scraper.daily_pilot import GraphQLGate
from test_instagram_permalink_verification import media, NOW
from test_instagram_daily_pilot import data


class CompoundIdentityBoundaryTests(unittest.TestCase):
    def blocked(self, value):
        with self.assertRaises(ValueError) as caught:
            normalize_identity(value)
        self.assertEqual(str(caught.exception), 'unsupported_media_identity_layout')

    def test_well_formed_compound_with_matching_raw_numeric_fields_is_not_proof(self):
        value=media();value.update(id='12345_67890',user={'username':'dvcatucsd','pk':'67890','id':67890})
        before=copy.deepcopy(value)
        self.assertEqual(inspect_identity(value)['id_shape'],'compound_numeric')
        self.blocked(value)
        self.assertEqual(value,before)

    def test_different_hypothetical_media_component_stays_unsupported(self):
        value=media();value.update(id='99999_67890',user={'username':'dvcatucsd','pk':'67890'})
        self.blocked(value)

    def test_different_hypothetical_account_component_stays_unsupported(self):
        value=media();value.update(id='12345_99999',user={'username':'dvcatucsd','pk':'67890'})
        self.blocked(value)

    def test_missing_author_identity_never_inferred_from_suffix(self):
        for user in (None,{}, {'username':'dvcatucsd'}):
            value=media();value.update(id='12345_67890',user=user)
            self.blocked(value)

    def test_payload_claims_of_confirmation_cannot_enable_compound_parsing(self):
        value=media();value.update(id='12345_67890',compound_id_verified=True,
            media_id_verified=True,accepted=True,
            user={'username':'dvcatucsd','id':'67890','verified':True})
        self.blocked(value)

    def test_malformed_compound_layouts_never_pass(self):
        for identity in ('12345_', '_67890', '12345_67890_7', '12345__67890',
                         '12345_-67890', '12345_0', '0_67890', '１２３４５_67890',
                         '12345_67890 ', '9'*33+'_67890'):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):normalize_identity({'pk':'12345','id':identity})

    def test_numeric_aliases_remain_supported_and_numeric_conflicts_reject(self):
        self.assertEqual(normalize_identity({'pk':12345,'id':'00012345'}),'12345')
        self.assertEqual(normalize_identity({'pk':'12345'}),'12345')
        with self.assertRaisesRegex(ValueError,'conflicting_media_identity'):
            normalize_identity({'pk':'12345','id':'67890'})

    def test_compound_child_does_not_inherit_account_identity_from_parent(self):
        for suffix,child_user in (('67890',None),('99999',None),
                                  ('67890',{'pk':'67890'}),('99999',{'pk':'99999'})):
            value=media();value.update(media_type=8,carousel_media_count=1,
                user={'username':'dvcatucsd','pk':'67890'},carousel_media=[dict(
                    pk='201',id='201_'+suffix,media_type=1,
                    display_url='https://test.cdninstagram.com/child.jpg')])
            if child_user is not None:value['carousel_media'][0]['user']=child_user
            with self.assertRaisesRegex(Unverified,'unsupported_media_identity_layout'):_normalize(value)

    def test_numeric_carousel_children_need_no_invented_same_author_rule(self):
        value=media();value.update(media_type=8,carousel_media_count=2,
            user={'username':'dvcatucsd','pk':'67890'},carousel_media=[
                dict(pk='201',id=201,media_type=1,user={'pk':'67890'},display_url='https://test.cdninstagram.com/a.jpg'),
                dict(pk='202',id=202,media_type=1,user={'pk':'99999'},display_url='https://test.cdninstagram.com/b.jpg')])
        _,images=_normalize(value)
        self.assertEqual(len(images),2)
        # This is numeric identity/media validation only, not source/author proof.

    def test_shortcode_and_other_daily_safety_gates_remain_separate(self):
        value=media();value.update(id='12345_67890')
        gate=GraphQLGate(now=NOW)
        gate.observe(data(value,errors=[{'path':['profile_grid','edges',0,'node','PRIVATE_UNKNOWN']}]),'graphql_error')
        summary=gate.summary()
        self.assertEqual(summary['accepted'],0)
        for reason in ('source_verification_unavailable','graphql_errors_unresolved_for_post',
                       'unsupported_media_identity_layout'):
            self.assertEqual(summary['reason_counts'][reason],1)
        for private in ('12345','67890','PRIVATE_UNKNOWN'):self.assertNotIn(private,json.dumps(summary))
        value['shortcode']='DifferentCode'
        with self.assertRaisesRegex(Unverified,'conflicting_post_identity'):_normalize(value)
