"""Offline-only daily pilot: existing shapes are not fabricated source evidence."""
import contextlib
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_instagram_timeline_media import media as timeline_fixture
from test_instagram_scrape_health import response
from app.instagram_scraper import schema_capture
from app.instagram_scraper import daily_pilot as pilot
import run_instagram_daily as cli

NOW=datetime(2026,11,5,20,tzinfo=timezone.utc)
ROOT='xdt_api__v1__feed__user_timeline_graphql_connection'


def image():
    # Reuse the existing observed-wrapper-shaped fixture, not a claimed real
    # response or verified profile. Explicit type/text here are synthetic inputs.
    value=timeline_fixture()
    value.update(media_type=1,user={'username':'dvcatucsd'},caption={'text':'Workshop November 20, 2026.'})
    return value


def data(*nodes,errors=None):
    result={'data':{ROOT:{'edges':[{'node':node} for node in nodes]}}}
    if errors is not None:result['errors']=errors
    return result


class DailyGraphQLTests(unittest.TestCase):
    def gate(self,payload):
        gate=pilot.GraphQLGate(now=NOW);gate.observe(payload,'graphql_error')
        result=gate.summary()
        self.assertEqual(result['accepted'],0)
        for private in ('dvcatucsd','safe_code','PRIVATE_CAPTION','private.jpg','SECRET'):
            self.assertNotIn(private,json.dumps(result))
        return gate,result

    def test_legacy_flat_image_does_not_manufacture_source_proof(self):
        _,result=self.gate(data(image()))
        self.assertEqual(result['image_fields_valid'],1)
        self.assertEqual(result['reason_counts']['source_verification_unavailable'],1)

    def test_observed_nested_wrapper_and_three_null_slots_are_independent_skips(self):
        _,result=self.gate(data({'media':image()},*({'media':None} for _ in range(3))))
        self.assertEqual(result['observations'],4)
        self.assertEqual(result['image_fields_valid'],1)
        self.assertEqual(result['reason_counts']['media_null_placeholder'],3)

    def test_unknown_graphql_paths_never_become_harmless(self):
        _,result=self.gate(data(image(),errors=[{'path':[ROOT,'edges',0,'node','SECRET_FIELD']}]))
        self.assertEqual(result['reason_counts']['graphql_errors_unresolved_for_post'],1)

    def test_videos_mixed_carousels_and_unknown_media_skip(self):
        video=image();video['media_type']=2
        unknown=image();unknown.pop('media_type')
        mixed=image();mixed.update(media_type=8,carousel_media_count=2,carousel_media=[
            {'pk':'201','media_type':1,'display_url':'https://test.cdninstagram.com/a.jpg'},
            {'pk':'202','media_type':2,'display_url':'https://test.cdninstagram.com/b.jpg'}])
        _,result=self.gate(data(video,unknown,mixed))
        self.assertEqual(result['image_fields_valid'],0)
        self.assertEqual(result['skipped'],3)

    def test_complete_image_only_carousel_still_requires_source(self):
        carousel=image();carousel.update(media_type=8,carousel_media_count=2,carousel_media=[
            {'pk':'201','media_type':1,'display_url':'https://test.cdninstagram.com/a.jpg'},
            {'pk':'202','media_type':1,'display_url':'https://test.cdninstagram.com/b.jpg'}])
        _,result=self.gate(data(carousel))
        self.assertEqual(result['image_fields_valid'],1)
        self.assertEqual(result['accepted'],0)

    def test_collaboration_hints_or_payload_confirmation_flags_cannot_authorize(self):
        value=image();value.update(user={'username':'other_account'},
            coauthor_producers=[{'username':'dvcatucsd','accepted':True}],verified_match=True)
        _,result=self.gate(data(value))
        self.assertEqual(result['reason_counts']['accepted_collaboration_unverified'],1)

    def test_existing_freshness_and_event_screening_run_before_paid_work(self):
        value=image();value['taken_at']=int((NOW-timedelta(days=41)).timestamp())
        _,result=self.gate(data(value))
        self.assertEqual(result['reason_counts']['post_outside_freshness_window'],1)
        value=image();value['caption']['text']='Workshop October 24, 2026.'
        _,result=self.gate(data(value))
        self.assertEqual(result['reason_counts']['event_expired'],1)

    def test_duplicates_are_counted_without_merging_or_writing(self):
        _,result=self.gate(data(image(),image()))
        self.assertEqual(result['duplicate_observations'],1)
        self.assertEqual(result['skipped'],2)

    def test_malformed_items_do_not_hide_valid_shape_peers(self):
        _,result=self.gate(data({'media':[]},{'media_or_ad':image()},image()))
        self.assertEqual(result['image_fields_valid'],1)
        self.assertEqual(result['skipped'],3)

    def test_capture_observer_uses_database_free_path_and_preserves_health(self):
        original=schema_capture.capture
        gate=pilot.GraphQLGate(now=NOW)
        def isolated(*args,**kwargs):
            return original(*args,**kwargs,partial_capture=True,post_observer=gate.observe)
        import test_instagram_schema_capture as harness
        value=data(image(),errors=[{'path':[ROOT,'edges',0,'node','SECRET']}])
        with patch.object(harness,'capture',side_effect=isolated):
            result,*_=harness.CaptureOnlyTests().invoke(response(value))
        self.assertEqual(gate.summary()['skipped'],1)
        self.assertEqual(result['database_writes'],0)
        self.assertEqual(result['scrape_health']['status'],'failed')
        self.assertEqual(result['status'],'partial_unverified')


class DailyRunnerTests(unittest.TestCase):
    def test_default_preflight_has_no_lock_browser_database_or_run_journal(self):
        with patch.object(pilot,'single_run_lock') as lock,patch.object(schema_capture,'capture') as capture:
            result=pilot.execute()
        self.assertEqual(result['status'],'preflight');self.assertFalse(result['ready'])
        lock.assert_not_called();capture.assert_not_called()

    def test_disabled_and_enabled_flags_cannot_bypass_missing_real_proof(self):
        for enabled,status in (('false','disabled'),('true','blocked')):
            with patch.dict(os.environ,{'INSTAGRAM_DAILY_ENABLED':enabled}),patch.object(schema_capture,'capture') as capture:
                result=pilot.execute(run=True,gemini_cap=1,cover_cap=1)
            self.assertEqual(result['status'],status);capture.assert_not_called()
            self.assertEqual(result['gemini_http_requests'],0)
            self.assertEqual(result['openai_generation_requests'],0)

    def test_capture_validation_only_zero_limits(self):
        for gemini,cover in ((1,0),(0,1),(1,1)):
            with self.assertRaises(ValueError):pilot.execute(validate_capture=True,gemini_cap=gemini,cover_cap=cover)

    def test_non_target_profile_is_rejected(self):
        with self.assertRaises(ValueError):pilot.execute(profile='another_profile')

    def test_pilot_caps_cannot_exceed_one_or_expand_capture_scope(self):
        for options in (dict(gemini_cap=2),dict(cover_cap=2),dict(max_scrolls=3)):
            with self.assertRaises(ValueError):pilot.execute(**options)

    def test_concurrent_capture_is_busy_before_browser_or_observer(self):
        with patch.object(pilot,'single_run_lock',return_value=contextlib.nullcontext(False)),patch.object(schema_capture,'capture') as capture:
            result=pilot.execute(validate_capture=True)
        self.assertEqual(result['status'],'busy');capture.assert_not_called()

    def test_file_lock_prevents_overlap_in_another_process(self):
        with tempfile.TemporaryDirectory() as directory:
            with pilot.single_run_lock(directory) as held:
                self.assertTrue(held)
                command='from app.instagram_scraper.daily_pilot import single_run_lock\nwith single_run_lock('+repr(directory)+') as held: print(int(held))'
                result=subprocess.run([sys.executable,'-c',command],capture_output=True,timeout=10,text=True)
                self.assertEqual(result.returncode,0);self.assertEqual(result.stdout.strip(),'0')
            with pilot.single_run_lock(directory) as held:self.assertTrue(held)

    def test_insecure_lock_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o777)
            try:
                with self.assertRaises(ValueError):
                    with pilot.single_run_lock(directory):pass
            finally:Path(directory).chmod(0o700)

    def validate(self,reason='response_capture_failed',payload=None):
        def capture(*args,**kwargs):
            if payload is not None:kwargs['post_observer'](payload,'graphql_error')
            return dict(scrape_health=dict(status='failed',reason=reason,error_categories={}),elapsed_seconds=1)
        with patch.object(pilot,'single_run_lock',return_value=contextlib.nullcontext(True)),patch.object(schema_capture,'capture',side_effect=capture) as call:
            result=pilot.execute(validate_capture=True)
        self.assertEqual(call.call_args.args,('dvcatucsd',))
        self.assertTrue(call.call_args.kwargs['headless']);self.assertTrue(call.call_args.kwargs['partial_capture'])
        return result

    def test_partial_timeline_failures_produce_skips_not_global_failure(self):
        result=self.validate(payload=data(image(),{'media':None}))
        self.assertEqual(result['status'],'completed_with_skips')
        self.assertEqual(result['per_post']['skipped'],2)
        self.assertEqual(result['scrape_health']['status'],'failed')

    def test_auth_failure_and_missing_capture_remain_failures(self):
        self.assertEqual(self.validate(reason='login_required')['status'],'failed')
        self.assertEqual(self.validate()['status'],'failed')

    def test_valid_empty_connection_is_no_paid_noop(self):
        result=self.validate(reason='timeline_verified',payload=data())
        self.assertEqual(result['status'],'completed_with_skips')
        self.assertEqual(result['per_post']['skipped'],0)

    def test_cli_summary_and_zero_budget_validation(self):
        output=io.StringIO()
        with patch('dotenv.load_dotenv'),patch.object(pilot,'execute',return_value={'status':'preflight'}) as run, \
             contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'preflight')
        self.assertEqual(run.call_args.kwargs['gemini_cap'],0)
        with contextlib.redirect_stderr(io.StringIO()),patch.object(pilot,'execute') as run:
            with self.assertRaises(SystemExit):cli.main(['--validate-capture','--cover-max-attempts','1'])
        run.assert_not_called()

    def test_readiness_failure_has_non_success_exit_without_importer_or_paid_calls(self):
        with patch('dotenv.load_dotenv'),patch.dict(os.environ,{'INSTAGRAM_DAILY_ENABLED':'true'}), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--run']),78)
