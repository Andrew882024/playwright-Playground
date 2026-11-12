"""Opt-in schema evidence and privacy tests; all responses and CLI runs mocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from test_instagram_event_extraction import serial
from test_instagram_graphql_attribution import ROOT,payload
from test_instagram_scrape_health import response
import test_instagram_timeline_media as media_tests
from app.instagram_scraper import schema_probe as probe, sync_ins as scraper
import diagnose_instagram_graphql_schema as cli

FIELD='coauthor_producers'


def error_payload(field=FIELD):
    data=payload(media_tests.media())
    data['errors']=[{'path':[ROOT,'edges',0,'node',field],'message':'PRIVATE_MESSAGE'}]
    return data


def request_document(field=FIELD):
    return 'query Timeline($private: String) { '+ROOT+' { edges { node { '+field+' } } } }'


def reply(data=None,query=None):
    result=response(data or error_payload())
    result.request=Mock(post_data=json.dumps({'query':query or request_document(),
                                             'variables':{'cookie':'PRIVATE_COOKIE'}}))
    return result


class SchemaProbeTests(unittest.TestCase):
    def test_disabled_probe_never_inspects_request(self):
        value=reply();value.request=Mock()
        with patch.object(probe,'_query',side_effect=AssertionError('not enabled')):
            probe.observe(value,error_payload())
        # Default capture remains fail-closed and has no schema-name output.
        health=scraper.new_capture_health();bodies=[];codes=set()
        handler,_=scraper._make_graphql_posts_capture(bodies,codes,health)
        handler(value)
        self.assertEqual(health['valid_timeline_responses'],0)
        self.assertNotIn(FIELD,json.dumps(health));self.assertEqual(bodies,[])

    def test_validated_static_candidate_stays_in_memory_until_explicit_review(self):
        session=probe.Probe();session.observe(reply(),error_payload())
        self.assertEqual(session.counts['static_candidate'],1)
        self.assertNotIn(FIELD,json.dumps(session.summary()))
        self.assertEqual(session.review(lambda *_:False)['reviewed_fields'],[])
        fixture=session.review(lambda *_:True)
        self.assertEqual(fixture['reviewed_fields'][0],dict(field=FIELD,placement='flat',
            observations=1,evidence='unaliased_query_selection'))
        for private in ('PRIVATE_MESSAGE','PRIVATE_COOKIE','PRIVATE_CAPTION',ROOT,'safe_code','private.jpg'):
            self.assertNotIn(private,json.dumps(fixture))

    def test_aliases_at_field_or_ancestor_rejected(self):
        for query in (request_document(FIELD+': like_count'),
                      '{ '+ROOT+': other_root { edges { node { '+FIELD+' } } } }',
                      '{ '+ROOT+' { edges { node: other_node { '+FIELD+' } } } }'):
            session=probe.Probe();session.observe(reply(query=query),error_payload())
            self.assertEqual(session.counts['alias_rejected'],1)
            self.assertEqual(session.review(lambda *_:True)['reviewed_fields'],[])

    def test_sensitive_and_dynamic_identifiers_rejected(self):
        for field in ('account_12345','PRIVATE_TEST','user@example.com','signed/url',
                      'deadbeefdeadbeef','averylongidentifierwithoutbreaks','access_token','session_secret','password_hint','private_username','a'*49,'x'):
            data=error_payload(field);session=probe.Probe()
            session.observe(reply(data,request_document(field)),data)
            self.assertEqual(session.counts['unsafe_identifier'],1)
            fixture=session.review(lambda *_:True)
            self.assertEqual(fixture['reviewed_fields'],[])
            if len(field)>2:self.assertNotIn(field,json.dumps(fixture))

    def test_persisted_query_cannot_establish_schema_identity(self):
        session=probe.Probe();value=reply()
        value.request.post_data=json.dumps({'doc_id':'PRIVATE_DOC','variables':{'secret':'PRIVATE_SECRET'}})
        session.observe(value,error_payload())
        self.assertEqual(session.counts['query_unavailable'],1)
        self.assertNotIn('PRIVATE',json.dumps(session.summary()))
        self.assertEqual(session.review(lambda *_:True)['reviewed_fields'],[])

    def test_query_mismatch_is_rejected_not_guessed(self):
        session=probe.Probe();session.observe(reply(query=request_document('different_field')),error_payload())
        self.assertEqual(session.counts['query_path_mismatch'],1)

    def test_fragments_directives_multiple_operations_and_malformed_documents_rejected(self):
        for query in ('{ '+ROOT+' { ...Frag } } fragment Frag on Root { '+FIELD+' }',
                      request_document(FIELD+' @include(if: true)'),
                      request_document()+' '+request_document(),
                      '{ broken','mutation { remove }','"PRIVATE_QUERY"',
                      'query '+'x'*70000):
            session=probe.Probe();session.observe(reply(query=query),error_payload())
            self.assertEqual(session.counts['unsupported_query'],1)
            self.assertEqual(session.counts['static_candidate'],0)

    def test_strings_and_arguments_are_not_used_as_field_evidence(self):
        query='{ '+ROOT+'(value: "PRIVATE_SECRET } alias: stolen {", config: {key: [1,2]}) { edges { node { '+FIELD+' } } } }'
        session=probe.Probe();session.observe(reply(query=query),error_payload())
        self.assertEqual(session.counts['static_candidate'],1)
        self.assertNotIn('PRIVATE_SECRET',json.dumps(session.review(lambda *_:True)))

    def test_nested_media_prefix_is_resolved_exactly(self):
        data=error_payload();data['errors'][0]['path'].insert(-1,'media')
        query='{ '+ROOT+' { edges { node { media { '+FIELD+' } } } } }'
        session=probe.Probe();session.observe(reply(data,query),data)
        fixture=session.review(lambda *_:True)
        self.assertEqual(fixture['reviewed_fields'][0]['placement'],'media')

    def test_malformed_and_long_paths_do_not_expose_tokens(self):
        for path in (None,[],[ROOT,'edges',True,'node',FIELD],
                     [ROOT,'edges',-1,'node',FIELD],['PRIVATE_ROOT','edges',0,'node',FIELD],
                     [ROOT]+['PRIVATE_PATH']*40):
            data=error_payload();data['errors'][0]['path']=path
            session=probe.Probe();session.observe(reply(data),data)
            self.assertEqual(session.counts['malformed_or_unrecognized_path'],1)
            self.assertNotIn('PRIVATE',json.dumps(session.review(lambda *_:True)))

    def test_capture_remains_rejected_even_with_valid_query_evidence(self):
        session=probe.Probe();health=scraper.new_capture_health();bodies=[];codes=set()
        with probe.enabled(session):
            handler,_=scraper._make_graphql_posts_capture(bodies,codes,health)
        # Event callbacks may execute in another greenlet/context. The observer
        # must be bound when registered, rather than looked up during dispatch.
        from contextvars import Context
        Context().run(handler,reply())
        self.assertEqual(session.counts['static_candidate'],1)
        self.assertEqual(scraper.capture_failure(health),'response_capture_failed')
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())
        self.assertNotIn(FIELD,json.dumps(health))

    def test_probe_does_not_retain_payload_or_query(self):
        session=probe.Probe();session.observe(reply(),error_payload())
        state=repr(vars(session))
        for value in ('PRIVATE_MESSAGE','PRIVATE_COOKIE','PRIVATE_CAPTION',ROOT,'private.jpg','variables'):
            self.assertNotIn(value,state)

    def test_per_response_and_candidate_limits(self):
        data=error_payload();data['errors']*=140
        session=probe.Probe();session.observe(reply(data),data)
        self.assertEqual(session.counts['observed'],128);self.assertEqual(session.counts['omitted_errors'],12)
        for suffix in ('a','b','c','d','e','f','g','h','i'):
            field='schema_'+suffix;data=error_payload(field)
            session.observe(reply(data,request_document(field)),data)
        self.assertEqual(session.summary()['candidate_count'],8)
        self.assertGreater(session.counts['candidate_limit'],0)

    def test_form_query_and_body_limits(self):
        from urllib.parse import urlencode
        session=probe.Probe();value=reply();value.request.post_data=urlencode({'query':request_document(),'variables':'PRIVATE'})
        session.observe(value,error_payload())
        self.assertEqual(session.counts['static_candidate'],1)
        value.request.post_data='PRIVATE'*20000;session=probe.Probe();session.observe(value,error_payload())
        self.assertEqual(session.counts['query_unavailable'],1)

    def test_probe_failure_is_safe_and_context_is_restored(self):
        session=probe.Probe();value=reply()
        with patch.object(probe,'_query',side_effect=RuntimeError('PRIVATE_EXCEPTION')):
            session.observe(value,error_payload())
        self.assertEqual(session.counts['inspection_error'],1)
        self.assertNotIn('PRIVATE_EXCEPTION',json.dumps(session.summary()))
        with self.assertRaises(RuntimeError):
            with probe.enabled(session):raise RuntimeError()
        probe.observe(value,error_payload())
        self.assertEqual(session.counts['inspection_error'],1)


class DiagnosticCliTests(unittest.TestCase):
    def test_default_is_read_only_no_discovery_invocation(self):
        with patch('run_instagram_discovery_pipeline.main') as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--profile','dvcatucsd']),0)
        run.assert_not_called()

    def test_capture_only_never_uses_discovery_or_retries(self):
        for status,code in (('completed',0),('failed',1),('interrupted',1)):
            with patch('app.instagram_scraper.schema_capture.capture',return_value={'status':status}) as capture, \
                 patch('run_instagram_discovery_pipeline.main') as discovery,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--profile','dvcatucsd','--headless','--capture']),code)
            capture.assert_called_once_with('dvcatucsd',headless=True)
            discovery.assert_not_called()

    def test_noninteractive_review_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as directory,patch('run_instagram_discovery_pipeline.main') as run, \
             patch('sys.stdin.isatty',return_value=False),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):cli.main(['--profile','dvcatucsd','--capture','--review',
                                                       '--fixture',str(Path(directory)/'review.json')])
        run.assert_not_called()

    def test_fixture_write_is_private_exclusive_and_reviewed_only(self):
        session=probe.Probe();session.observe(reply(),error_payload())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.json'
            cli.save_fixture(path,session.review(lambda *_:True))
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            self.assertEqual(json.loads(path.read_text())['reviewed_fields'][0]['field'],FIELD)
            self.assertNotIn('PRIVATE',path.read_text())
            with self.assertRaises(FileExistsError):cli.save_fixture(path,{})
