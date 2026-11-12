"""Synthetic request hints cannot establish an unreviewed operation's target."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch
from urllib.parse import urlencode

from test_instagram_event_extraction import serial
from test_instagram_partial_capture import media, NOW
from test_instagram_graphql_attribution import payload
from test_instagram_scrape_health import response
import test_instagram_schema_capture as capture_tests
from app.instagram_scraper import request_profile_binding as binding
from app.instagram_scraper.partial_capture import PartialCapture


def reply(variables=None, *, form=False, raw=None):
    body={'doc_id':'PRIVATE_DOCUMENT','operationName':'PRIVATE_OPERATION'}
    if variables is not None:body['variables']=json.dumps(variables) if form else variables
    encoded=raw if raw is not None else urlencode(body) if form else json.dumps(body)
    return SimpleNamespace(request=SimpleNamespace(method='POST',post_data=encoded,
        headers={'content-type':'application/x-www-form-urlencoded' if form else 'application/json',
                 'cookie':'PRIVATE_COOKIE'}))


class RequestProfileBindingTests(unittest.TestCase):
    def inspect(self,value):
        result=binding.classify(value,'dvcatucsd')
        self.assertIn(result['binding'],binding.BINDINGS)
        self.assertIn(result['reason'],binding.REASONS)
        self.assertIn(result['username_hint_relation'],binding.RELATIONS)
        for secret in ('dvcatucsd','PRIVATE','different_org','987654321'):
            self.assertNotIn(secret,json.dumps(result))
        return result

    def test_matching_username_hint_is_not_verified_for_opaque_operation(self):
        for form in (False,True):
            result=self.inspect(reply({'username':'DVCATUCSD'},form=form))
            self.assertEqual(result['binding'],'ambiguous')
            self.assertEqual(result['username_hint_relation'],'matches_requested')
            self.assertEqual(result['reason'],'unreviewed_target_selector')

    def test_mismatching_username_hint_is_not_verified_mismatch(self):
        result=self.inspect(reply({'username':'different_org'}))
        self.assertEqual(result['binding'],'ambiguous')
        self.assertEqual(result['username_hint_relation'],'different_account')

    def test_conflicting_selectors_are_ambiguous(self):
        result=self.inspect(reply({'username':'dvcatucsd','target_username':'different_org'}))
        self.assertEqual(result['username_hint_relation'],'conflicting')
        self.assertEqual(result['binding'],'ambiguous')

    def test_numeric_target_cannot_be_bound_without_independent_mapping(self):
        result=self.inspect(reply({'id':'987654321'}))
        self.assertTrue(result['numeric_target_hint'])
        self.assertEqual(result['username_hint_relation'],'missing_or_invalid')
        self.assertEqual(result['binding'],'ambiguous')

    def test_missing_variables_and_request_are_unavailable(self):
        self.assertEqual(self.inspect(reply())['reason'],'variables_missing')
        self.assertEqual(self.inspect(SimpleNamespace(request=None))['binding'],'unavailable')
        self.assertEqual(self.inspect(reply(raw=''))['reason'],'body_unavailable')

    def test_request_accessor_failure_is_safe(self):
        obj=Mock()
        with patch.object(type(obj),'request',new_callable=PropertyMock,create=True,
                          side_effect=RuntimeError('PRIVATE_ERROR')):
            self.assertEqual(self.inspect(obj)['binding'],'unavailable')

    def test_malformed_duplicate_and_batched_metadata_remain_ambiguous(self):
        cases=[reply(raw='{PRIVATE'),reply(raw='[]'),reply(raw='{"variables":false}'),
            reply(raw='{"variables":{"username":"dvcatucsd","username":"different_org"}}'),
            reply(form=True,raw='variables=%7B%7D&variables=%7B%7D'),
            reply(form=True,raw='variables=%ZZ'),reply(raw='{"variables":"{PRIVATE"}')]
        for value in cases:
            with self.subTest(value=cases.index(value)):
                self.assertEqual(self.inspect(value)['reason'],'malformed_metadata')

    def test_wrong_variable_types_are_not_target_evidence(self):
        for value in (True,17,None,[],{'username':'dvcatucsd'},'PRIVATE invalid username'):
            result=self.inspect(reply({'username':value}))
            self.assertEqual(result['binding'],'ambiguous')
            self.assertEqual(result['username_hint_relation'],'missing_or_invalid')

    def test_no_recursive_inspection_or_unknown_selector_inference(self):
        result=self.inspect(reply({'input':{'username':'dvcatucsd'},'PRIVATE_SELECTOR':'dvcatucsd'}))
        self.assertEqual(result['reason'],'variables_without_target_hint')
        self.assertEqual(result['username_hint_relation'],'missing_or_invalid')

    def test_bounded_inspection(self):
        self.assertEqual(self.inspect(reply(raw='x'*131073))['reason'],'inspection_limit')
        self.assertEqual(self.inspect(reply(raw=json.dumps({'variables':'x'*65537})))['reason'],
                         'inspection_limit')
        self.assertEqual(self.inspect(reply({str(n):0 for n in range(129)}))['reason'],'inspection_limit')

    def test_inline_query_or_operation_name_alone_is_not_reviewed_contract(self):
        value=reply(raw=json.dumps({'query':'query($username:String!){user(username:$username){id}}',
                                  'variables':{'username':'dvcatucsd'}}))
        self.assertEqual(self.inspect(value)['binding'],'ambiguous')

    def test_unsupported_methods_and_encodings_are_unavailable(self):
        value=reply({'username':'dvcatucsd'});value.request.method='GET'
        self.assertEqual(self.inspect(value)['binding'],'unavailable')
        value=reply();value.request.headers['content-type']='multipart/form-data'
        self.assertEqual(self.inspect(value)['reason'],'unsupported_encoding')

    def test_cross_tab_preserves_author_mismatch_and_coauthor_hint(self):
        session=PartialCapture('dvcatucsd',now=NOW)
        nodes=[media(),media()]
        nodes[1]['user']={'username':'different_org'}
        nodes[1]['coauthor_producers']=[{'username':'dvcatucsd'}]
        for node in nodes:
            with session.request_context(reply({'username':'dvcatucsd'})):
                session.observe(payload(node),'graphql_error')
        result=session.summary()
        self.assertEqual(result['request_binding_counts']['ambiguous'],2)
        self.assertEqual(result['counts']['fully_valid_candidates'],1)
        patterns=result['candidate_provenance'];self.assertEqual(len(patterns),2)
        self.assertTrue(all(p['request_binding']=='ambiguous' for p in patterns))
        self.assertEqual(patterns[0]['validation_outcome'],'visible_fields_valid')
        self.assertEqual(patterns[1]['validation_outcome'],'incomplete')
        self.assertTrue(patterns[1]['requested_account_in_coauthor_hint'])
        self.assertEqual(patterns[1]['author_relation'],'different_account')
        self.assertNotIn('dvcatucsd',json.dumps(result));self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual(result['status'],'partial_unverified')

    def test_context_is_restored_on_nested_callbacks_exception_and_interruption(self):
        session=PartialCapture('dvcatucsd',now=NOW)
        with session.request_context(reply({'username':'dvcatucsd'})):
            with self.assertRaises(KeyboardInterrupt):
                with session.request_context(reply({'username':'different_org'})):raise KeyboardInterrupt()
            session.observe(payload(media()),'valid_timeline')
        self.assertIsNone(session._request_binding)
        self.assertEqual(session.summary()['candidate_provenance'][0]['request_username_hint_relation'],
                         'matches_requested')

    def test_capture_wires_same_response_request_without_database_or_downstream(self):
        original=capture_tests.capture
        value=response(payload(media()));value.request=reply({'username':'dvcatucsd'},form=True).request
        def partial(*args,**kwargs):return original(*args,**kwargs,partial_capture=True)
        with patch.object(capture_tests,'capture',side_effect=partial):
            result,*_=capture_tests.CaptureOnlyTests().invoke(value)
        self.assertEqual(result['partial_capture']['request_binding_counts']['ambiguous'],1)
        self.assertEqual(result['partial_capture']['candidate_provenance'][0]['request_username_hint_relation'],
                         'matches_requested')
        self.assertEqual(result['database_writes'],0);self.assertEqual(result['downstream'],'not_invoked')
        self.assertEqual(result['gemini_http_requests'],0);self.assertEqual(result['openai_generation_requests'],0)

    def test_non_instagram_response_never_reads_request_metadata(self):
        original=capture_tests.capture
        value=response(payload(media()));value.url='https://unrelated.example/graphql'
        def partial(*args,**kwargs):return original(*args,**kwargs,partial_capture=True)
        with patch.object(capture_tests,'capture',side_effect=partial), \
             patch('app.instagram_scraper.request_profile_binding.classify',side_effect=AssertionError('forbidden')) as inspect:
            result,*_=capture_tests.CaptureOnlyTests().invoke(value)
        inspect.assert_not_called()
        self.assertEqual(sum(result['partial_capture']['request_binding_counts'].values()),0)
