"""Fixed field diagnostics only; no raw paths, values, network or live state."""
import json
import unittest

from test_instagram_event_extraction import serial
from test_instagram_graphql_attribution import ROOT, payload
import test_instagram_timeline_media as media_tests
from app.instagram_scraper import graphql_diagnostics as diag


def path(*tail):
    return [ROOT,'edges',0,'node',*tail]


class ConsumedFieldTests(unittest.TestCase):
    capture=media_tests.TimelineMediaTests.capture

    def test_new_image_structures_and_descendants(self):
        for tail in (['display_resources',0,'src'],
                     ['edge_sidecar_to_children','edges',0,'node','display_url'],
                     ['edge_sidecar_to_children','edges',0,'node','image_versions2','candidates',0,'url']):
            for wrapper in ([],['media'],['media_or_ad'],['media_or_ad','media']):
                with self.subTest(tail=tail,wrapper=wrapper):
                    result=diag.classify_error_path(path(*wrapper,*tail))
                    self.assertEqual(result['category'],'timeline_media')
                    self.assertEqual(result['first_media_field'],tail[0])
                    self.assertFalse(result['contains_unknown_segments'])

    def test_all_directly_consumed_node_fields_have_fixed_labels(self):
        consumed={
            'timeline_identity':('code','id','pk'),
            'timeline_time':('taken_at','device_timestamp'),
            'timeline_caption':('headline','accessibility_caption','caption'),
            'timeline_organizer':('user',),
            'timeline_comments':('edge_media_to_comment','preview_comments'),
            'timeline_pinning':('is_pinned','is_pinned_to_profile'),
            'timeline_media':('display_url','thumbnail_src','video_url','image_versions2',
                              'image_versions','display_resources','carousel_media',
                              'edge_sidecar_to_children','video_versions'),
        }
        for category,fields in consumed.items():
            for field in fields:
                result=diag.classify_error_path(path(field))
                self.assertEqual(result['category'],category)
                self.assertEqual(result['first_media_field'],field)

    def test_consumed_descendant_fields(self):
        for tail,category in ((['caption','text'],'timeline_caption'),
            (['user','username'],'timeline_organizer'),
            (['edge_media_to_comment','count'],'timeline_comments'),
            (['preview_comments',0,'node','owner','username'],'timeline_comments'),
            (['preview_comments',0,'created_at'],'timeline_comments'),
            (['preview_comments',0,'id'],'timeline_comments'),
            (['preview_comments',0,'text'],'timeline_comments')):
            result=diag.classify_error_path(path(*tail))
            self.assertEqual(result['category'],category)
            self.assertFalse(result['contains_unknown_segments'])
            self.assertEqual(result['first_media_field'],tail[0])

    def test_pagination_remains_connection_scoped_not_media_field(self):
        for field in diag.PAGINATION_FIELDS:
            result=diag.classify_error_path([ROOT,field])
            self.assertEqual(result['category'],'timeline_pagination')
            self.assertEqual(result['first_media_field'],'unmatched')

    def test_first_field_never_searches_past_an_unknown_ancestor(self):
        for tail in (['PRIVATE_TEST','display_resources'],['PRIVATE_TEST','caption','text']):
            result=diag.classify_error_path(path(*tail))
            self.assertEqual(result['first_media_field'],'unmatched')
            self.assertEqual(result['category'],'timeline_unknown_field')
            self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_unknown_descendant_retains_first_known_field_and_uncertainty(self):
        result=diag.classify_error_path(path('display_resources',0,'PRIVATE_TEST'))
        self.assertEqual(result['first_media_field'],'display_resources')
        self.assertTrue(result['unknown_below_required_field'])
        self.assertTrue(result['contains_unknown_segments'])
        self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_malformed_unrecognized_and_overlong_paths_are_unmatched(self):
        for value in (None,[],{},'PRIVATE_TEST',path(True),path({}),
                      [ROOT,'edges',-1,'node','caption'],['PRIVATE_TEST','edges',0,'node','caption'],
                      path('caption',*(['PRIVATE_TEST']*40)),[ROOT,'edges',0,'node']):
            result=diag.classify_error_path(value)
            self.assertEqual(result['first_media_field'],'unmatched')
            self.assertNotIn('PRIVATE_TEST',json.dumps(result))

    def test_bounded_counts_include_unmatched_and_do_not_accept_errors(self):
        data=payload(media_tests.media())
        data['errors']=[{'path':path('display_resources',0,'src'),'message':'PRIVATE_TEST'},
                        {'path':path('media','caption','text')},
                        {'path':path('PRIVATE_TEST')}, {'path':None}, 'PRIVATE_TEST']
        health,bodies,codes=self.capture(data)
        counts=health['graphql_error_diagnostics']['first_media_field_counts']
        self.assertEqual(counts['display_resources'],1)
        self.assertEqual(counts['caption'],1);self.assertEqual(counts['unmatched'],3)
        self.assertEqual(sum(counts.values()),5)
        self.assertEqual(health['valid_timeline_responses'],0)
        self.assertEqual(bodies,[]);self.assertEqual(codes,set())
        self.assertNotIn('PRIVATE_TEST',json.dumps(health))
        data['errors']=[{'path':path('display_resources')}]*140
        health,_,_=self.capture(data)
        details=health['graphql_error_diagnostics']
        self.assertEqual(details['first_media_field_counts']['display_resources'],128)
        self.assertEqual(details['omitted_error_items'],12)

    def test_aggregation_reprojects_fixed_keys_and_counts(self):
        raw={'graphql_error_diagnostics':{'first_media_field_counts':
             {'display_resources':2,'unmatched':1,'PRIVATE_TEST':100,'caption':True,'user':-1}}}
        result=diag.aggregate_diagnostics([raw,raw])
        counts=result['graphql_error_diagnostics']['first_media_field_counts']
        self.assertEqual(counts['display_resources'],4);self.assertEqual(counts['unmatched'],2)
        self.assertEqual(counts['caption'],0);self.assertEqual(counts['user'],0)
        self.assertNotIn('PRIVATE_TEST',json.dumps(result))
        self.assertEqual(set(counts),set(diag.FIRST_MEDIA_FIELD_ENUMS))

    def test_durable_log_retains_only_fixed_field_counts(self):
        from test_instagram_execution_log import ExecutionLogTests
        from app.parent_pipeline import execution_log as log
        fixture=ExecutionLogTests();fixture.setUp()
        try:
            data=payload(media_tests.media())
            data['errors']=[{'path':path('display_resources',0,'src'),'message':'PRIVATE_TEST'}]
            health,_,_=self.capture(data)
            def execute():
                log.begin('scraping');log.finish('scraping',dict(failed=1,scrape_health=health))
                return dict(status='failed')
            record=fixture.read(log.invoke(execute))
            counts=record['stages']['scraping']['items'][0]['graphql_error_diagnostics']['first_media_field_counts']
            self.assertEqual(counts['display_resources'],1)
            self.assertNotIn('PRIVATE_TEST',json.dumps(record))
        finally:
            fixture.doCleanups()
