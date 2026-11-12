"""Offline per-model configuration and SDK serialization regressions."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.genai import models, types
from test_instagram_event_extraction import ga, row_six, TZ


class GeminiGenerationConfigTests(unittest.TestCase):
    def test_modern_models_omit_temperature_from_sdk_payload(self):
        for model in ('gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.8-flash',
                      'gemini-3.8-pro-preview', 'gemini-3.9-flash', 'gemini-3.10-flash',
                      'gemini-4-argon', 'gemini-4.1-flash', 'gemini-10-flash',
                      'models/gemini-3.8-flash'):
            with self.subTest(model=model):
                config = ga._generation_config(model)
                self.assertIsNone(config.temperature)
                self.assertNotIn('temperature', config.model_dump(exclude_unset=True))
                params = types._GenerateContentParameters(model=model, contents='offline', config=config)
                # Pure SDK conversion: no client, HTTP transport, or API call.
                payload = models._GenerateContentParameters_to_mldev(
                    SimpleNamespace(vertexai=False), params, None, params)
                self.assertNotIn('temperature', payload['generationConfig'])
                self.assertEqual(payload['generationConfig']['responseMimeType'], 'application/json')
                self.assertEqual(config.system_instruction, ga._SYSTEM_INSTRUCTION)

    def test_older_and_unversioned_models_preserve_existing_sampling(self):
        for model in ('gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-2.5-flash',
                      'gemini-2.5-flash-lite', 'gemini-3-flash-preview',
                      'gemini-3.1-flash-lite-preview', 'gemini-3.5-flash',
                      'models/gemini-2.5-flash', 'unknown-model'):
            with self.subTest(model=model):
                config = ga._generation_config(model)
                self.assertEqual(config.temperature, 0.2)
                self.assertEqual(config.response_mime_type, 'application/json')
                self.assertEqual(config.system_instruction, ga._SYSTEM_INSTRUCTION)

    def test_dispatch_and_fallback_use_each_models_own_config(self):
        for chain in (['gemini-3.8-flash', 'gemini-2.5-flash'],
                      ['gemini-2.5-flash', 'gemini-3.8-flash']):
            with self.subTest(chain=chain):
                client = Mock()
                error = ga.ServerError(503, {'error': {'code': 503}})
                client.models.generate_content.side_effect = [
                    error, error, error, SimpleNamespace(text=json.dumps(row_six()))]
                with patch.object(ga.time, 'sleep'), contextlib.redirect_stderr(io.StringIO()):
                    _, index = ga.analyze_post(client, chain, {}, TZ)
                self.assertEqual(index, 1)
                calls = client.models.generate_content.call_args_list
                self.assertEqual([c.kwargs['model'] for c in calls], [chain[0]]*3+[chain[1]])
                for call in calls:
                    expected = None if call.kwargs['model'] == 'gemini-3.8-flash' else 0.2
                    self.assertEqual(call.kwargs['config'].temperature, expected)
                    self.assertEqual(call.kwargs['config'].response_mime_type, 'application/json')
                self.assertEqual(calls[0].kwargs['contents'], calls[-1].kwargs['contents'])
