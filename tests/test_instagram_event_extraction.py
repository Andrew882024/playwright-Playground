"""Offline contract and analyzer tests. All external calls are mocked."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from pydantic import ValidationError

# Prevent constructing a configured real engine, even though create_engine itself
# normally connects lazily. No database is needed to import ORM definitions.
with patch('sqlalchemy.create_engine', return_value=Mock(name='offline_engine')):
    serial = importlib.import_module('app.ai_analyze_service.ai_analyze')
    threaded = importlib.import_module('app.ai_analyze_service_mutithread.ai_analyze_mt')
from app.ai_analyze_service import gemini_analysis as ga
from app.ai_analyze_service_mutithread import gemini_analysis as mt
from app.ai_analyze_service_mutithread import redis_queue as queue

TZ = ZoneInfo('America/Los_Angeles')
# Synthetic row-6 scenario: expected data supplied by the user, not fetched from DB.
PROSE = ('This campus session invites students to meet members of the organizing group, '
         'learn about its activities, and participate in the advertised program. '
         'The description refers only to this individual session and its stated schedule. '
         'Attendees should consult the original Instagram announcement for any further '
         'details about participation and arrangements. This is a synthetic offline fixture, '
         'not a claim about the real event announcement.')


def row_six():
    def event(title, start, end, duration):
        def at(hour, minute):
            return dict(year=2026, month=10, day=20, hour=hour, minute=minute,
                        timezone_iana='America/Los_Angeles')
        return dict(event_title=title, provider_name='ACM at UCSD',
                    event_description=PROSE, location='UCSD', start=at(*start),
                    end=at(*end), duration_minutes=duration, confidence='high')
    return dict(description=PROSE, main_image_url=None, raw_notes=None, events=[
        event('Coffee Chats', (10, 0), (12, 0), 120),
        event('Tech Talk', (17, 0), (18, 30), 90),
    ])


class ExtractionTests(unittest.TestCase):
    def test_row_six_both_analyzers(self):
        for module in (ga, mt):
            with self.subTest(module=module.__name__):
                ai = module.PostAnalysisAI.model_validate(row_six())
                values = module.ai_to_child_event_values(ai, TZ)
                self.assertEqual([v['event_title'] for v in values], ['Coffee Chats', 'Tech Talk'])
                self.assertEqual([v['duration_in_minutes'] for v in values], [120, 90])
                self.assertEqual([v['event_start_at'].hour for v in values], [10, 17])
                self.assertEqual([(v['event_end_at'].hour, v['event_end_at'].minute) for v in values], [(12, 0), (18, 30)])
                self.assertEqual([v['event_start_at'].utcoffset().total_seconds() for v in values], [-25200, -25200])
                self.assertIn('2 event(s)', module.format_ai_terminal_summary('row6', ai))

    def test_parent_projection_zero_one_multiple_and_reorder(self):
        for module in (ga, mt):
            fixture = row_six()
            ai = module.PostAnalysisAI.model_validate(fixture)
            projected = module.ai_to_db_update_values(ai, TZ, fallback_main_image_url='parent.jpg')
            self.assertEqual(projected['event_title'], 'Coffee Chats')
            self.assertEqual(projected['event_end_at'].hour, 12)
            self.assertEqual(projected['duration_in_minutes'], 120)
            self.assertEqual(projected['main_image_url'], 'parent.jpg')
            fixture['events'].reverse()
            self.assertEqual(module.ai_to_db_update_values(module.PostAnalysisAI.model_validate(fixture), TZ,
                             fallback_main_image_url='parent.jpg'), projected)
            fixture['events'] = fixture['events'][:1]
            one = module.ai_to_db_update_values(module.PostAnalysisAI.model_validate(fixture), TZ)
            self.assertEqual(one['event_title'], 'Tech Talk')
            self.assertEqual(one['duration_in_minutes'], 90)
            fixture['events'] = []
            ai = module.PostAnalysisAI.model_validate(fixture)
            empty = module.ai_to_db_update_values(ai, TZ)
            self.assertFalse(ai.is_event)
            self.assertTrue(empty['ai_analyzed'])
            self.assertFalse(empty['is_event'])
            for key in ('event_title', 'provider_name', 'event_start_at', 'event_end_at', 'duration_in_minutes', 'location', 'confidence'):
                self.assertIsNone(empty[key])
            self.assertEqual(module.ai_to_child_event_values(ai, TZ), [])

    def test_missing_or_invalid_events_are_not_zero_events(self):
        for module in (ga, mt):
            for invalid in (None, {}, 'not an array', [None]):
                data = row_six(); data['events'] = invalid
                with self.subTest(module=module.__name__, invalid=invalid), self.assertRaises(ValidationError):
                    module.PostAnalysisAI.model_validate(data)
            data = row_six(); del data['events']
            with self.assertRaises(ValidationError):
                module.PostAnalysisAI.model_validate(data)
            data = row_six(); del data['events'][0]['location']
            with self.assertRaises(ValidationError):
                module.PostAnalysisAI.model_validate(data)

    def test_bad_dates_durations_and_unknown_times(self):
        for module in (ga, mt):
            for mutate in (
                lambda e: e['end'].update(hour=9),
                lambda e: e['start'].update(month=2, day=30),
                lambda e: e['start'].update(timezone_iana='Not/AZone'),
                lambda e: e.update(duration_minutes=1),
            ):
                data = row_six(); mutate(data['events'][0])
                with self.assertRaises(ValueError):
                    module.ai_to_child_event_values(module.PostAnalysisAI.model_validate(data), TZ)
            data = row_six(); data['events'][0]['start']['hour'] = None
            children = module.ai_to_child_event_values(module.PostAnalysisAI.model_validate(data), TZ)
            self.assertIsNone(children[0]['event_start_at'])

    def test_prompt_demands_separate_sessions(self):
        for module in (ga, mt):
            prompt = module._user_prompt({}, 'reference', 0, [])
            self.assertIn('events (REQUIRED array)', prompt)
            self.assertIn('Do NOT merge separate sessions', prompt)
            self.assertIn('TWO items', prompt)
            self.assertIn('independently attendable', module._SYSTEM_INSTRUCTION)

    def test_mocked_gemini_success_both_entry_points(self):
        for module, single in ((ga, False), (mt, False), (mt, True)):
            client = Mock()
            client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(row_six()))
            if single:
                ai = module.analyze_post_single_model(client, 'model', {'shortcode': 'row6'}, TZ)
            else:
                ai, idx = module.analyze_post(client, ['model'], {'shortcode': 'row6'}, TZ)
                self.assertEqual(idx, 0)
            self.assertEqual(len(ai.events), 2)
            self.assertEqual(ai.gemini_model, 'model')
            self.assertEqual(client.models.generate_content.call_count, 1)

    def test_schema_retry_and_model_fallback(self):
        for module in (ga, mt):
            client = Mock()
            client.models.generate_content.side_effect = [SimpleNamespace(text='{}'), SimpleNamespace(text=json.dumps(row_six()))]
            with patch.object(module.time, 'sleep'):
                ai, _ = module.analyze_post(client, ['model'], {}, TZ)
            self.assertEqual(len(ai.events), 2)
            self.assertEqual(client.models.generate_content.call_count, 2)
            with patch.object(module, '_generate_content_with_429_retry', side_effect=[
                RuntimeError('free-tier quota exhausted'), SimpleNamespace(text=json.dumps(row_six()))
            ]) as generate:
                _, idx = module.analyze_post(client, ['first', 'second'], {}, TZ)
            self.assertEqual(idx, 1)
            self.assertEqual([c.args[1] for c in generate.call_args_list], ['first', 'second'])

    def test_transient_retry_bound_and_permanent_errors(self):
        error = ga.ServerError(503, {'error': {'code': 503, 'message': 'high demand'}})
        client = Mock(); content = ['same request']
        client.models.generate_content.side_effect = [error, error, 'ok']
        with patch.object(ga.time, 'sleep') as sleep:
            self.assertEqual(ga._generate_content_with_429_retry(client, 'model', content, None), 'ok')
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])
        self.assertTrue(all(c.kwargs['contents'] is content for c in client.models.generate_content.call_args_list))
        client.models.generate_content.reset_mock(side_effect=True)
        client.models.generate_content.side_effect = error
        with patch.object(ga.time, 'sleep'), self.assertRaises(ga.ServerError):
            ga._generate_content_with_429_retry(client, 'model', content, None)
        self.assertEqual(client.models.generate_content.call_count, 3)
        for code in (400, 401, 403, 404):
            client = Mock(); client.models.generate_content.side_effect = ga.ClientError(code, {'error': {'code': code}})
            with patch.object(ga.time, 'sleep') as sleep, self.assertRaises(ga.ClientError):
                ga._generate_content_with_429_retry(client, 'model', [], None)
            self.assertEqual(client.models.generate_content.call_count, 1)
            sleep.assert_not_called()

    def test_exhausted_transient_model_falls_back_and_second_model_succeeds(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                error = ga.ServerError(code, {'error': {'code': code}})
                client = Mock()
                client.models.generate_content.side_effect = [error, error, error,
                    SimpleNamespace(text=json.dumps(row_six()))]
                with patch.object(ga.time, 'sleep') as sleep:
                    ai, idx = ga.analyze_post(client, ['first', 'second'], {'shortcode': 'row6'}, TZ)
                self.assertEqual(idx, 1)
                self.assertEqual(ai.gemini_model, 'second')
                self.assertEqual(len(ai.events), 2)
                self.assertEqual([c.kwargs['model'] for c in client.models.generate_content.call_args_list],
                                 ['first', 'first', 'first', 'second'])
                self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])

    def test_auth_and_configuration_errors_never_fall_back(self):
        for code in (400, 401, 403, 404):
            client = Mock()
            client.models.generate_content.side_effect = ga.ClientError(code, {'error': {'code': code}})
            with patch.object(ga.time, 'sleep') as sleep, self.assertRaises(ga.ClientError):
                ga.analyze_post(client, ['first', 'second'], {}, TZ)
            self.assertEqual(client.models.generate_content.call_count, 1)
            self.assertEqual(client.models.generate_content.call_args.kwargs['model'], 'first')
            sleep.assert_not_called()

    def test_all_transient_models_exhaust_with_clean_failure(self):
        client = Mock()
        client.models.generate_content.side_effect = ga.ServerError(503, {'error': {'code': 503}})
        with patch.object(ga.time, 'sleep'), self.assertRaises(RuntimeError) as failure:
            ga.analyze_post(client, ['first', 'second'], {}, TZ)
        self.assertEqual(str(failure.exception), ga.ALL_MODELS_EXHAUSTED_MSG)
        self.assertEqual([c.kwargs['model'] for c in client.models.generate_content.call_args_list],
                         ['first'] * 3 + ['second'] * 3)

    def test_limit_counts_failed_posts_and_no_persistence_on_failure(self):
        rows = [SimpleNamespace(post_shortcode=str(i)) for i in range(3)]
        for limit in (0, 1, 2, None):
            with patch.object(serial, 'fetch_instagram_rows_for_profile', return_value=rows), \
                 patch.object(serial, 'build_analyzer_post_dict', side_effect=lambda r: {'shortcode': r.post_shortcode}), \
                 patch.object(serial, 'analyze_post', side_effect=RuntimeError('failure')) as analyze, \
                 patch.object(serial, 'persist_analysis') as persist:
                serial.process_profile_db('acm.at.ucsd', client=Mock(), model_chain=['m'], tz=TZ,
                                          limit=limit, only_not_analyzed=True, sleep_s=0)
            self.assertEqual(analyze.call_count, 3 if limit is None else limit)
            persist.assert_not_called()

    def test_serial_passes_all_children_in_one_persistence_call(self):
        ai = ga.PostAnalysisAI.model_validate(row_six())
        with patch.object(serial, 'fetch_instagram_rows_for_profile', return_value=[SimpleNamespace(post_shortcode='row6')]), \
             patch.object(serial, 'build_analyzer_post_dict', return_value={}), \
             patch.object(serial, 'analyze_post', return_value=(ai, 0)), \
             patch.object(serial, 'persist_analysis', return_value=True) as persist:
            serial.process_profile_db('acm.at.ucsd', client=Mock(), model_chain=['m'], tz=TZ,
                                      limit=1, only_not_analyzed=False, sleep_s=0)
        persist.assert_called_once()
        self.assertEqual(len(persist.call_args.args[4]), 2)
        self.assertTrue(persist.call_args.kwargs['reanalyze'])

    def test_legacy_json_failure_is_not_successful_empty_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'posts.json').write_text(json.dumps({'posts': [{'shortcode': 'failed'}]}))
            with patch.object(serial, 'PROJECT_ROOT', directory), \
                 patch.object(serial, 'analyze_post', side_effect=RuntimeError('offline failure')):
                serial.process_profile_dir(directory, client=Mock(), model_chain=['m'], tz=TZ,
                                           limit=1, resume=False, sleep_s=0)
            ai = json.loads((directory / 'posts_ai.json').read_text())['posts'][0]['ai']
            self.assertTrue(ai['analysis_failed'])
            self.assertIn('error', ai)
            self.assertNotIn('events', ai)
            self.assertEqual(serial._load_existing_ai_by_shortcode(directory / 'posts_ai.json'), {})

    def test_job_reanalysis_survives_serialization_and_reassignment(self):
        job = queue.AnalyzeJob('acm.at.ucsd', 'row6', reanalyze=True)
        restored = queue.AnalyzeJob.from_json(job.to_json())
        self.assertTrue(restored.reanalyze)
        old = queue.AnalyzeJob.from_json('{"profile_username":"acm.at.ucsd","post_shortcode":"row6"}')
        self.assertFalse(old.reanalyze)
        redis = Mock()
        self.assertTrue(queue.reassign_job(redis, queue.MODEL_QUEUE_CHAIN[0], 'raw', restored))
        moved = queue.AnalyzeJob.from_json(redis.lpush.call_args.args[1])
        self.assertTrue(moved.reanalyze)
        self.assertEqual(moved.attempted_models, [queue.MODEL_QUEUE_CHAIN[0]])

    def test_enqueue_carries_reanalysis_intent(self):
        with patch.object(threaded, 'fetch_all_candidate_post_keys', return_value=[('acm.at.ucsd', 'row6')]), \
             patch.object(threaded, 'enqueue_job') as enqueue:
            threaded.enqueue_candidates_from_db(Mock(), only_not_analyzed=False,
                profile_username='acm.at.ucsd', limit=1, flush_queues=False)
        self.assertTrue(enqueue.call_args.args[2].reanalyze)

    def test_worker_respects_reanalysis_and_commit_before_ack(self):
        row = SimpleNamespace(ai_analyzed=True)
        ai = mt.PostAnalysisAI.model_validate(row_six())
        with patch.object(threaded, 'fetch_instagram_row', return_value=row), \
             patch.object(threaded, 'build_analyzer_post_dict', return_value={}), \
             patch.object(threaded, 'analyze_post_single_model', return_value=ai) as analyze, \
             patch.object(threaded, 'persist_analysis', return_value=True) as persist:
            threaded.process_one_job(client=Mock(), model='model', job=queue.AnalyzeJob('a', 'b'), tz=TZ)
            analyze.assert_not_called()
            threaded.process_one_job(client=Mock(), model='model', job=queue.AnalyzeJob('a', 'b', reanalyze=True), tz=TZ)
            persist.assert_called_once()
            self.assertTrue(persist.call_args.kwargs['reanalyze'])
            self.assertEqual(len(persist.call_args.args[4]), 2)
        order = []
        job = queue.AnalyzeJob('a', 'b')
        with patch.object(threaded, 'redis_client', return_value=Mock()), \
             patch.object(threaded, 'claim_job', side_effect=[('raw', job), None]), \
             patch.object(threaded, 'queue_depths', return_value={'model': (0, 0)}), \
             patch.object(threaded, 'process_one_job', side_effect=lambda **kw: order.append('persist')), \
             patch.object(threaded, 'ack_job', side_effect=lambda *args: order.append('ack')):
            threaded._worker_loop('model', client=Mock(), tz=TZ, stop_event=Mock(is_set=Mock(return_value=False)), brpop_timeout=1)
        self.assertEqual(order, ['persist', 'ack'])

class PublicDescriptionPromptTests(unittest.TestCase):
    def test_both_prompts_require_concise_factual_prose_and_private_notes(self):
        from pathlib import Path
        for module in (ga, mt):
            instruction = module._SYSTEM_INSTRUCTION
            for phrase in ('date discrepancy warnings', 'fact-check commentary', 'uncertainty commentary',
                           'advice to verify with organizers', 'AI reasoning', 'invented information',
                           'raw_notes only', 'at most 60 words'):
                self.assertIn(phrase, instruction)
            source = Path(module.__file__).read_text()
            self.assertNotIn('50–200 words', source)
            self.assertNotIn('50–200 word post-level', source)
            data = row_six()
            data['description'] = 'ACM hosts campus activities.'
            data['events'][0]['event_description'] = 'Meet the team over coffee.'
            ai = module.PostAnalysisAI.model_validate(data)
            self.assertEqual(ai.events[0].event_description, 'Meet the team over coffee.')
