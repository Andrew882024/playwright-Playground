"""Synthetic provenance hints, not evidence of Instagram collaboration semantics."""
import copy
import json
import unittest

from test_instagram_event_extraction import serial
from test_instagram_partial_capture import media,NOW
from test_instagram_graphql_attribution import ROOT,payload
from test_instagram_scrape_health import response
from app.instagram_scraper.partial_capture import PartialCapture
from app.instagram_scraper import sync_ins as scraper


class PartialProvenanceTests(unittest.TestCase):
    def inspect(self,node=None,key=ROOT,errors=True):
        value=media() if node is None else node
        data={'data':{key:{'edges':[{'node':value}]}}}
        if errors:data['errors']=[{'message':'PRIVATE_ERROR'}]
        session=PartialCapture('dvcatucsd',now=NOW)
        session.observe(data,'graphql_error' if errors else 'valid_timeline')
        result=session.summary()
        for forbidden in ('dvcatucsd','other_org','PRIVATE_CODE','PRIVATE_CAPTION','PRIVATE_ERROR',
                          'PRIVATE_ROOT','private.jpg','12345'):
            self.assertNotIn(forbidden,json.dumps(result))
        return session,result['candidate_provenance'][0]

    def test_named_timeline_does_not_prove_request_profile_binding(self):
        session,pattern=self.inspect()
        self.assertEqual(pattern['connection_kind'],'known_timeline_root')
        self.assertEqual(pattern['author_relation'],'matches_requested')
        self.assertEqual(pattern['validation_outcome'],'visible_fields_valid')
        self.assertEqual(session.summary()['request_profile_binding'],'not_verified')
        self.assertFalse(session.summary()['production_eligible'])

    def test_unrelated_generic_media_is_distinguished_but_mismatch_not_accepted(self):
        node=media();node['user']={'username':'other_org'}
        session,pattern=self.inspect(node,key='PRIVATE_ROOT')
        self.assertEqual(pattern['connection_kind'],'generic_media_edges')
        self.assertEqual(pattern['author_relation'],'different_account')
        self.assertEqual(pattern['validation_outcome'],'incomplete')
        self.assertEqual(session.reasons['source_account_mismatch'],1)
        self.assertEqual(session.counts['fully_valid_candidates'],0)

    def test_matching_author_in_generic_connection_still_has_ambiguous_provenance(self):
        session,pattern=self.inspect(key='PRIVATE_ROOT')
        self.assertEqual(session.counts['fully_valid_candidates'],1)
        self.assertEqual(pattern['connection_kind'],'generic_media_edges')
        self.assertEqual(session.summary()['status'],'partial_unverified')
        self.assertEqual(session.summary()['request_profile_binding'],'not_verified')

    def test_timeline_name_hint_is_not_exact_known_root(self):
        _,pattern=self.inspect(key='PRIVATE_ROOT_timeline')
        self.assertEqual(pattern['connection_kind'],'timeline_name_hint')

    def test_collaboration_hint_never_overrides_primary_author_mismatch(self):
        node=media();node['user']={'username':'other_org'}
        node['coauthor_producers']=[{'username':'dvcatucsd','id':'PRIVATE_ID'}]
        session,pattern=self.inspect(node)
        self.assertEqual(pattern['coauthor_shape'],'user_object_list')
        self.assertTrue(pattern['requested_account_in_coauthor_hint'])
        self.assertEqual(session.summary()['collaboration_semantics'],'unverified_hints_only')
        self.assertEqual(session.reasons['source_account_mismatch'],1)
        self.assertEqual(session.counts['fully_valid_candidates'],0)
        self.assertNotIn('PRIVATE_ID',json.dumps(session.summary()))

    def test_invited_collaborator_is_not_treated_as_accepted_coauthor(self):
        node=media();node['user']={'username':'other_org'}
        node['invited_coauthors']=[{'username':'dvcatucsd'}]
        session,pattern=self.inspect(node)
        self.assertEqual(pattern['coauthor_shape'],'absent')
        self.assertFalse(pattern['requested_account_in_coauthor_hint'])
        self.assertEqual(session.counts['fully_valid_candidates'],0)

    def test_owner_match_does_not_replace_mismatched_user_field(self):
        node=media();node['user']={'username':'other_org'};node['owner']={'username':'dvcatucsd'}
        session,pattern=self.inspect(node)
        self.assertEqual(pattern['owner_relation'],'matches_requested')
        self.assertEqual(pattern['owner_author_relation'],'disagree')
        self.assertEqual(session.reasons['source_account_mismatch'],1)
        self.assertEqual(session.counts['fully_valid_candidates'],0)

    def test_conflicting_owner_is_reported_even_when_user_passes_existing_field_check(self):
        node=media();node['owner']={'username':'other_org'}
        session,pattern=self.inspect(node)
        self.assertEqual(pattern['author_relation'],'matches_requested')
        self.assertEqual(pattern['owner_relation'],'different_account')
        self.assertEqual(pattern['owner_author_relation'],'disagree')
        self.assertEqual(session.counts['fully_valid_candidates'],1)  # Existing check unchanged.
        self.assertFalse(session.summary()['production_eligible'])

    def test_owner_agreement_and_absence_are_distinct(self):
        _,absent=self.inspect()
        self.assertEqual(absent['owner_author_relation'],'unavailable')
        node=media();node['owner']={'username':'DVCATUCSD'}
        _,agreed=self.inspect(node)
        self.assertEqual(agreed['owner_author_relation'],'agree')

    def test_malformed_and_truncated_coauthor_metadata_is_not_authoritative(self):
        for values,shape in ((None,'null'),([], 'empty_list'),('PRIVATE_VALUE','malformed'),
                             ([{'username':False}],'malformed'),
                             ([{'username':'other_org'}]*33,'truncated')):
            node=media();node['coauthor_producers']=values
            session,pattern=self.inspect(node)
            self.assertEqual(pattern['coauthor_shape'],shape)
            self.assertEqual(pattern['coauthor_inspection_truncated'],shape=='truncated')
            self.assertFalse(pattern['requested_account_in_coauthor_hint'])
            self.assertNotIn('PRIVATE_VALUE',json.dumps(session.summary()))

    def test_patterns_count_observations_not_unique_posts_and_do_not_retain_values(self):
        session=PartialCapture('dvcatucsd',now=NOW);data=payload(media())
        for _ in range(3):session.observe(data,'valid_timeline')
        self.assertEqual(session.counts['unique_post_identities'],1)
        self.assertEqual(session.counts['fully_valid_candidates'],3)
        patterns=session.summary()['candidate_provenance']
        self.assertEqual(len(patterns),1);self.assertEqual(patterns[0]['count'],3)
        self.assertFalse(patterns[0]['response_has_errors'])
        self.assertNotIn('PRIVATE',repr(session._provenance_patterns))

    def test_nested_wrapper_and_error_response_keep_production_rejection(self):
        node=media();data=payload({'media':node});data['errors']=[{'message':'PRIVATE_ERROR'}]
        session=PartialCapture('dvcatucsd',now=NOW)
        health=scraper.new_capture_health();bodies=[];codes=set()
        handler,_=scraper._make_graphql_posts_capture(bodies,codes,health,diagnostic_payload=session.observe)
        handler(response(data))
        pattern=session.summary()['candidate_provenance'][0]
        self.assertEqual(pattern['wrapper_kind'],'node_media');self.assertTrue(pattern['response_has_errors'])
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())

    def test_unrelated_non_media_responses_and_nested_unconfirmed_roots_are_not_extracted(self):
        for data,category in (({'data':{'inbox':{'edges':[]}}},'ignored_graphql_response'),
            ({'data':{'user':{ROOT:{'edges':[{'node':media()}]}}}},'unsupported_timeline_schema')):
            session=PartialCapture('dvcatucsd',now=NOW);session.observe(data,category)
            self.assertEqual(session.counts['candidate_media_nodes'],0)
            self.assertEqual(session.summary()['candidate_provenance'],[])
