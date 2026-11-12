"""Offline, fixed transport projections; no actual request/response traffic."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import PropertyMock,Mock,patch
from urllib.parse import urlencode

from app.instagram_scraper import query_transport as transport
from app.instagram_scraper import schema_probe
from test_instagram_schema_probe import error_payload,reply,request_document


class QueryTransportTests(unittest.TestCase):
    def inspect(self,body,content_type=None,method='POST'):
        request=SimpleNamespace(post_data=body,method=method,
                                headers={'content-type':content_type,'authorization':'PRIVATE_HEADER'} if content_type else {})
        result=transport.inspect_request(SimpleNamespace(request=request))
        self.assertNotIn('PRIVATE',json.dumps(result[1]))
        return result

    def test_inline_json(self):
        query,shape=self.inspect(json.dumps({'query':request_document(),'operationName':'PRIVATE_OPERATION',
            'variables':{'cookie':'PRIVATE_COOKIE'}}),'application/json; charset=utf-8')
        self.assertEqual(query,request_document());self.assertEqual(shape['transport'],'inline_query')
        self.assertEqual(shape['encoding'],'json');self.assertTrue(shape['has_operation_name'])
        self.assertEqual(shape['method'],'POST')

    def test_inline_form(self):
        query,shape=self.inspect(urlencode({'query':request_document(),'variables':'PRIVATE_VARIABLES'}),
                                 'application/x-www-form-urlencoded')
        self.assertEqual(query,request_document());self.assertEqual(shape['encoding'],'form')
        self.assertEqual(shape['transport'],'inline_query')

    def test_doc_id_json_and_form(self):
        for body,mime in ((json.dumps({'doc_id':'PRIVATE_DOC'}),'application/json'),
                          (urlencode({'doc_id':'PRIVATE_DOC'}),'application/x-www-form-urlencoded')):
            query,shape=self.inspect(body,mime)
            self.assertIsNone(query);self.assertEqual(shape['transport'],'persisted_doc_id')
            self.assertTrue(shape['has_doc_id'])

    def test_persisted_query_hash_json_and_form(self):
        ext={'persistedQuery':{'version':1,'sha256Hash':'PRIVATE_HASH'}}
        for body,mime in ((json.dumps({'extensions':ext}),'application/json'),
                          (urlencode({'extensions':json.dumps(ext)}),'application/x-www-form-urlencoded'),
                          (json.dumps({'query_hash':'PRIVATE_HASH'}),'application/json'),
                          (urlencode({'queryHash':'PRIVATE_HASH'}),'application/x-www-form-urlencoded')):
            query,shape=self.inspect(body,mime)
            self.assertIsNone(query);self.assertEqual(shape['transport'],'persisted_hash')
            self.assertTrue(shape['has_persisted_query_hash'])

    def test_mixed_and_inline_plus_persisted_metadata(self):
        values={'doc_id':'PRIVATE_DOC','query_hash':'PRIVATE_HASH'}
        _,shape=self.inspect(json.dumps(values));self.assertEqual(shape['transport'],'persisted_mixed')
        values['query']=request_document()
        query,shape=self.inspect(json.dumps(values))
        self.assertEqual(shape['transport'],'inline_query');self.assertEqual(query,request_document())
        self.assertTrue(shape['has_doc_id']);self.assertTrue(shape['has_persisted_query_hash'])

    def test_operation_names_are_presence_only_not_query_evidence(self):
        for key,flag in (('operationName','has_operation_name'),
                         ('fb_api_req_friendly_name','has_friendly_operation_name')):
            _,shape=self.inspect(urlencode({key:'PRIVATE_OPERATION'}))
            self.assertEqual(shape['transport'],'operation_only');self.assertTrue(shape[flag])

    def test_absent_body_and_request_access_failures(self):
        _,shape=transport.inspect_request(SimpleNamespace(request=None))
        self.assertEqual(shape['transport'],'request_unavailable')
        value=Mock()
        with patch.object(type(value),'request',new_callable=PropertyMock,create=True,
                          side_effect=RuntimeError('PRIVATE')):
            _,shape=transport.inspect_request(value)
        self.assertEqual(shape['transport'],'request_access_error')
        value=Mock()
        with patch.object(type(value),'post_data',new_callable=PropertyMock,create=True,
                          side_effect=RuntimeError('PRIVATE')):
            _,shape=transport.inspect_request(SimpleNamespace(request=value))
        self.assertEqual(shape['transport'],'body_access_error')
        _,shape=self.inspect(None,method='GET')
        self.assertEqual(shape['transport'],'body_unavailable');self.assertEqual(shape['method'],'GET')
        self.assertTrue(shape['request_available']);self.assertFalse(shape['body_present'])

    def test_body_decoding_failure_has_safe_encoding_category(self):
        request=Mock()
        with patch.object(type(request),'post_data',new_callable=PropertyMock,create=True,
                          side_effect=UnicodeDecodeError('utf-8',b'\xff',0,1,'PRIVATE')):
            query,shape=transport.inspect_request(SimpleNamespace(request=request))
        self.assertIsNone(query);self.assertEqual(shape['transport'],'unsupported_encoding')
        self.assertNotIn('PRIVATE',json.dumps(shape))

    def test_unsupported_encodings(self):
        for body,mime in ((b'PRIVATE_BINARY',None),('query PRIVATE { field }','application/graphql'),
                          ('--PRIVATE_BOUNDARY','multipart/form-data; boundary=PRIVATE')):
            _,shape=self.inspect(body,mime)
            self.assertEqual(shape['transport'],'unsupported_encoding')

    def test_oversized_malformed_batch_and_non_object_bodies(self):
        for body,mime,category in (('x'*131073,None,'body_too_large'),
            ('{"PRIVATE":','application/json','malformed_body'),
            ('query=%ZZ','application/x-www-form-urlencoded','malformed_body'),
            ('query=%FF','application/x-www-form-urlencoded','malformed_body'),
            ('[{"doc_id":"PRIVATE"}]','application/json','batched_json_unsupported'),
            ('null','application/json','json_non_object')):
            _,shape=self.inspect(body,mime);self.assertEqual(shape['transport'],category)

    def test_invalid_or_duplicate_query_fields_rejected(self):
        for body,mime,category in ((json.dumps({'query':17}),None,'query_invalid'),
            (json.dumps({'query':''}),None,'query_invalid'),
            ('query=',None,'query_invalid'),('query=a&query=b',None,'query_ambiguous'),
            ('{"query":"a","query":"b"}',None,'malformed_body')):
            query,shape=self.inspect(body,mime)
            self.assertIsNone(query);self.assertEqual(shape['transport'],category)

    def test_form_limits_and_invalid_extensions(self):
        _,shape=self.inspect('&'.join('field%d=x'%i for i in range(129)))
        self.assertEqual(shape['transport'],'malformed_body')
        for ext in ('PRIVATE_INVALID','x'*16385):
            _,shape=self.inspect(urlencode({'extensions':ext}))
            self.assertTrue(shape['extensions_invalid'])
            self.assertFalse(shape['has_persisted_query_hash'])
            self.assertEqual(shape['transport'],'no_query_metadata')

    def test_transport_is_counted_per_response_not_per_error(self):
        session=schema_probe.Probe();data=error_payload();data['errors']*=28
        value=reply(data);value.request.post_data=json.dumps({'doc_id':'PRIVATE_DOC'})
        session.observe(value,data)
        summary=session.summary()
        self.assertEqual(summary['counts']['query_unavailable'],28)
        self.assertEqual(summary['query_transport']['responses'],1)
        self.assertEqual(summary['query_transport']['transport_counts']['persisted_doc_id'],1)
        self.assertNotIn('PRIVATE',json.dumps(summary))
        self.assertNotIn('PRIVATE',repr(vars(session)))
        self.assertNotIn('PRIVATE',json.dumps(session.review(lambda *_:True)))

    def test_transport_errors_do_not_authorize_a_candidate(self):
        for body in (None,json.dumps({'doc_id':'PRIVATE'}),'query=a&query=b'):
            session=schema_probe.Probe();value=reply();value.request.post_data=body
            session.observe(value,error_payload())
            self.assertEqual(session.counts['static_candidate'],0)
            self.assertEqual(session.review(lambda *_:True)['reviewed_fields'],[])
