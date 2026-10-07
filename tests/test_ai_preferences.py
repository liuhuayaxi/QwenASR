from tests import isolation

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.ai_secrets import AISecretStore


class PreferencesTests(unittest.TestCase):
    def test_preferences_survive_restart_and_keep_keys_out_of_settings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            secrets = AISecretStore(root / 'keys.json')
            preferences = AIPreferences(root / 'preferences.json')
            saved = preferences.save({'detailed_summary_prompt': '保留课程公式', 'api_key': 'must-not-leak',
                'function_providers': {'summary': {'base_url': 'https://api.siliconflow.cn/v1',
                    'model': 'deepseek-ai/DeepSeek-V3.2', 'api_key': 'dummy-secret', 'concurrency': 3,
                    'context_window': 256000, 'extra_params': '{"temperature":0.2}'}}}, secrets)
            reloaded = AIPreferences(preferences.path).read()
            self.assertEqual(reloaded, saved)
            self.assertEqual(reloaded['function_providers']['summary']['model'], 'deepseek-ai/DeepSeek-V3.2')
            self.assertNotIn('dummy-secret', json.dumps(reloaded))
            self.assertNotIn('must-not-leak', json.dumps(reloaded))
            self.assertEqual(preferences.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(AISecretStore(secrets.path).get('summary', 'https://api.siliconflow.cn/v1'), 'dummy-secret')
            self.assertEqual(secrets.get('summary', 'https://api.openai.com/v1'), '')
            preferences.save(reloaded, secrets)
            self.assertEqual(secrets.get('summary', 'https://api.siliconflow.cn/v1'), 'dummy-secret')

    def test_invalid_settings_do_not_replace_saved_preferences(self):
        with tempfile.TemporaryDirectory() as raw:
            preferences = AIPreferences(Path(raw) / 'settings.json')
            preferences.path.write_text('{"existing":true}')
            for providers in ({}, {'summary': {'base_url': 'not-a-url'}}):
                with self.assertRaises(ValueError):
                    preferences.save({'function_providers': providers}, AISecretStore(Path(raw)/'keys.json'))
            self.assertEqual(preferences.read(), {'existing': True})
            preferences.path.write_text('damaged-json')
            with self.assertRaises(ValueError): preferences.read()


class PreferencesRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_api_restores_disk_without_returning_credentials(self):
        import app
        with tempfile.TemporaryDirectory() as raw:
            preferences = AIPreferences(Path(raw) / 'settings.json')
            secrets = AISecretStore(Path(raw) / 'keys.json')
            with patch.object(app, 'ai_preferences', preferences), patch.object(app, 'ai_secret_store', secrets):
                await app.save_ai_settings({'settings': {'function_providers': {'summary': {
                    'base_url': 'https://api.siliconflow.cn/v1', 'model': 'saved-model', 'api_key': 'dummy'}}}})
                self.assertEqual((await app.get_ai_settings())['settings']['function_providers']['summary']['model'], 'saved-model')
                self.assertNotIn('dummy', json.dumps(await app.get_ai_settings()))
                await app.clear_ai_settings()
                self.assertEqual((await app.get_ai_settings())['settings'], {})
                self.assertFalse(secrets.get('summary', 'https://api.siliconflow.cn/v1'))

    async def test_course_defaults_seed_from_disk_and_never_replace_existing_choices(self):
        from qwen_asr_app import smartclass_service as service
        with tempfile.TemporaryDirectory() as raw:
            preferences = AIPreferences(Path(raw) / 'settings.json')
            preferences.save({'function_providers': {'summary': {
                'base_url': 'https://api.siliconflow.cn/v1', 'model': 'saved-model'}}}, AISecretStore(Path(raw)/'keys.json'))
            with patch.object(service, 'ai_preferences', preferences), patch.object(service, 'options', {}), \
                    patch.object(service, 'STATE_DIR', Path(raw)):
                await service.defaults()
                self.assertEqual(service.options['summary_provider']['model'], 'saved-model')
                self.assertEqual(json.loads((Path(raw)/'options.json').read_text())['summary_provider']['model'], 'saved-model')
                service.options['summary_provider']['model'] = 'course-specific-model'
                await service.defaults()
                self.assertEqual(service.options['summary_provider']['model'], 'course-specific-model')
