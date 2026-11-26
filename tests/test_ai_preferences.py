from tests import isolation

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.ai_secrets import AISecretStore


class PreferencesTests(unittest.TestCase):
    def test_saved_cli_route_survives_browser_saves_and_explicit_clear(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);preferences=AIPreferences(root/'settings.json');secrets=AISecretStore(root/'keys.json')
            provider={'transport':'agy_cli','cli_model':'gemini-3.8-flash-high','cli_proxy_url':'http://127.0.0.1:7897'}
            preferences.save({'function_providers':{'summary':provider}},secrets)
            preferences.save({'function_providers':{'summary':{'transport':'agy_cli','cli_model':'gemini-3.8-flash-high'}}},secrets)
            self.assertEqual(preferences.task_config(required_modes=('summary',))['ai']['summary_provider']['cli_proxy_url'],'http://127.0.0.1:7897')
            preferences.save({'function_providers':{'summary':{**provider,'cli_proxy_url':''}}},secrets)
            self.assertEqual(preferences.read()['function_providers']['summary']['cli_proxy_url'],'')

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
    def test_task_retry_config_is_rebuilt_from_saved_disk_settings(self):
        import app
        with tempfile.TemporaryDirectory() as raw:
            preferences = AIPreferences(Path(raw) / 'settings.json')
            secrets = AISecretStore(Path(raw) / 'keys.json')
            providers = {
                mode: {'base_url': 'https://api.siliconflow.cn/v1', 'model': 'saved-' + mode,
                       'api_mode': 'chat_completions'}
                for mode in ('sentence', 'review', 'summary', 'correction', 'global')
            }
            preferences.save({'history_chars': 1200, 'function_providers': providers}, secrets)
            record = type('Record', (), {'metadata': {'course_settings': {'terminology': ['术语']}}})()
            with patch.object(app, 'ai_preferences', preferences):
                config = app._saved_ai_task_config(record, 'refinement')
            self.assertEqual(config['ai']['correction_provider']['model'], 'saved-correction')
            self.assertEqual(config['ai']['base_url'], 'https://api.siliconflow.cn/v1')
            self.assertNotIn('api_key', json.dumps(config))
            self.assertEqual(config['terminology'], ['术语'])

    def test_course_bridge_preserves_its_explicit_automatic_correction_flag(self):
        with tempfile.TemporaryDirectory() as raw:
            preferences = AIPreferences(Path(raw) / 'settings.json')
            secrets = AISecretStore(Path(raw) / 'keys.json')
            providers = {mode: {'base_url': 'https://api.siliconflow.cn/v1', 'model': 'saved-' + mode}
                         for mode in ('sentence', 'review', 'summary', 'correction', 'global')}
            preferences.save({'function_providers': providers}, secrets)
            config = preferences.task_config(
                {'ai_correction': True, 'summary_enabled': False, 'translation': 'off'},
                required_modes=('sentence', 'correction', 'summary'),
                preserve_flags=('ai_correction', 'summary_enabled', 'translation'),
            )
            self.assertTrue(config['ai_correction'])
            self.assertFalse(config['summary_enabled'])
            self.assertEqual(config['translation'], 'off')

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
                # The durable AI settings are authoritative across restarts;
                # stale course options cannot override them.
                self.assertEqual(service.options['summary_provider']['model'], 'saved-model')

    async def test_note_review_and_lightweight_routing_with_legacy_fallback(self):
        from unittest.mock import AsyncMock, patch
        from qwen_asr_app.ai import provider_for_mode, lightweight_mode_for, summarize_note_abstract, generate_recording_title
        with tempfile.TemporaryDirectory() as raw:
            preferences = AIPreferences(Path(raw) / 'settings.json')
            secrets = AISecretStore(Path(raw) / 'keys.json')
            preferences.save({
                'function_providers': {
                    'summary': {'transport': 'agy_cli', 'cli_model': 'gemini-4-hydrogen-medium', 'cli_effort': 'medium'},
                    'note_review': {'transport': 'agy_cli', 'cli_model': 'gemini-4-hydrogen-high', 'cli_effort': 'high'},
                    'correction': {'transport': 'agy_cli', 'cli_model': 'gemini-4-hydrogen-low', 'cli_effort': 'low'},
                }
            }, secrets)
            config = preferences.task_config(required_modes=('summary',))
            self.assertEqual(provider_for_mode(config['ai'], 'note_review')['cli_model'], 'gemini-4-hydrogen-high')
            self.assertEqual(provider_for_mode(config['ai'], 'summary')['cli_model'], 'gemini-4-hydrogen-medium')
            self.assertEqual(lightweight_mode_for(config, 'summary'), 'correction')
            mock_text = AsyncMock(return_value='短摘要')
            mock_json = AsyncMock(return_value={'title': '课堂标题'})
            with patch('qwen_asr_app.ai.request_ai_text', mock_text), patch('qwen_asr_app.ai.request_ai_json', mock_json):
                await summarize_note_abstract('笔记正文', config)
                await generate_recording_title('摘要内容', '取名提示', config)
            self.assertEqual(mock_text.await_args.kwargs['mode'], 'correction')
            self.assertEqual(mock_json.await_args.kwargs['mode'], 'correction')

            # Legacy saves without note_review or correction must still fall back cleanly to summary.
            legacy_prefs = AIPreferences(Path(raw) / 'legacy' / 'settings.json')
            legacy_prefs.save({
                'function_providers': {
                    'summary': {'transport': 'agy_cli', 'cli_model': 'gemini-4-hydrogen-medium', 'cli_effort': 'medium'},
                }
            }, AISecretStore(Path(raw) / 'legacy' / 'keys.json'))
            legacy = legacy_prefs.task_config(required_modes=('summary',))
            self.assertEqual(provider_for_mode(legacy['ai'], 'note_review')['cli_model'], 'gemini-4-hydrogen-medium')
            self.assertEqual(lightweight_mode_for(legacy, 'summary'), 'summary')

    async def test_regenerate_task_action_preserves_saved_agy_model(self):
        import app
        from unittest.mock import patch
        from qwen_asr_app.storage import RecordingRecord
        with tempfile.TemporaryDirectory() as raw:
            preferences = AIPreferences(Path(raw) / 'settings.json')
            secrets = AISecretStore(Path(raw) / 'keys.json')
            preferences.save({
                'function_providers': {
                    'correction': {'transport': 'agy_cli', 'cli_model': 'gemini-4-hydrogen-low', 'cli_effort': 'low', 'concurrency': 2},
                }
            }, secrets)
            rec_dir = Path(raw) / 'rec-regen'
            rec_dir.mkdir(parents=True, exist_ok=True)
            record = RecordingRecord(rec_dir, {'id': 'rec-regen', 'title': 'T', 'category': 'C', 'status': 'completed', 'sample_count': 0})
            record.upsert_segment({'id': 1, 'start': 0.0, 'end': 1.0, 'raw': '测试句子。'})
            captured = {}

            def fake_run(rec, cfg):
                captured['config'] = cfg
                return None

            def fake_start(rec, k, runner):
                runner()
                return True

            with patch.object(app, 'ai_preferences', preferences), \
                    patch.object(app.recording_store, 'find', return_value=record), \
                    patch.object(app, '_run_refinement_job', new=fake_run), \
                    patch.object(app, '_start_aux_ai_job', side_effect=fake_start):
                await app.ai_task_action({'recording_id': 'rec-regen', 'type': 'refinement', 'action': 'regenerate'})
            provider = captured['config']['ai']['correction_provider']
            self.assertEqual(provider['cli_model'], 'gemini-4-hydrogen-low')
            self.assertGreaterEqual(provider['concurrency'], 4)
