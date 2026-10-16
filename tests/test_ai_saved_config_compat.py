"""Use the user's durable choices across old schemas and partial saves."""
from tests import isolation  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.ai_secrets import AISecretStore


class SavedConfigCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.preferences=AIPreferences(self.root/'settings.json')
        self.secrets=AISecretStore(self.root/'secrets.json')

    def write(self,settings):
        self.preferences.path.write_text(json.dumps(settings))

    def test_old_root_sentence_fields_are_reused_without_browser_or_environment_defaults(self):
        saved={'base_url':'https://saved.example/v1','model':'saved-sentence-model',
            'api_mode':'chat_completions','context_window':131072,'extra_params':'{"temperature":0.1}',
            'ai_correction':True,'translation':'off','history_chars':1200}
        self.write(saved)
        original=self.preferences.path.read_bytes()
        with patch.dict('os.environ',{'AI_MODEL':'wrong-environment-model'}):
            result=self.preferences.task_config({'ai':{'base_url':'https://stale.example/v1','model':'wrong-browser-model'}},
                required_modes=('sentence',))
        self.assertEqual(result['ai']['base_url'],saved['base_url'])
        self.assertEqual(result['ai']['model'],saved['model'])
        self.assertEqual(result['ai']['context_window'],131072)
        self.assertEqual(result['_ai_settings_source'],'local_saved')
        self.assertTrue(result['ai_correction'])
        self.assertEqual(self.preferences.path.read_bytes(),original)

    def test_partial_new_map_can_reuse_explicit_legacy_sentence_and_review(self):
        self.write({'base_url':'https://saved.example/v1','model':'saved-sentence',
            'review_base_url':'https://review.example/v1','review_model':'saved-review',
            'function_providers':{'summary':{'transport':'agy_cli','cli_model':'saved-summary-high','cli_effort':'high'}}})
        result=self.preferences.task_config(required_modes=('sentence','review','summary'))
        self.assertEqual(result['ai']['model'],'saved-sentence')
        self.assertEqual(result['ai']['review_provider']['model'],'saved-review')
        self.assertEqual(result['ai']['summary_provider']['cli_model'],'saved-summary-high')
        self.assertEqual(result['ai']['summary_provider']['cli_effort'],'high')

    def test_explicit_canonical_mode_is_not_overridden_by_legacy_duplicate(self):
        self.write({'base_url':'https://old.example/v1','model':'old-model',
            'function_providers':{'sentence':{'transport':'agy_cli','cli_model':'saved-low','cli_effort':'high'}}})
        result=self.preferences.task_config(required_modes=('sentence',))
        self.assertEqual(result['ai']['transport'],'agy_cli')
        self.assertEqual(result['ai']['cli_model'],'saved-low')
        self.assertNotIn('base_url',result['ai'])
        self.write({'base_url':'https://old.example/v1','model':'old-model','function_providers':{'sentence':{}}})
        with self.assertRaisesRegex(ValueError,'尚未保存 sentence'):
            self.preferences.task_config(required_modes=('sentence',))

    def test_summary_model_is_never_borrowed_for_missing_sentence(self):
        self.write({'function_providers':{'summary':{'transport':'agy_cli','cli_model':'summary-only'}}})
        with self.assertRaisesRegex(ValueError,'尚未保存 sentence'):
            self.preferences.task_config(required_modes=('sentence',))

    def test_partial_save_preserves_other_modes_flags_prompts_and_bound_credentials(self):
        sentence={'base_url':'https://saved.example/v1','model':'saved-sentence','api_key':'dummy-key'}
        review={'transport':'agy_cli','cli_model':'saved-review-high','cli_effort':'high','concurrency':6}
        self.preferences.save({'ai_correction':True,'summary_enabled':True,'translation':'en',
            'detailed_summary_prompt':'saved prompt','function_providers':{'sentence':sentence,'review':review}},self.secrets)
        saved=self.preferences.save({'function_providers':{'summary':{'transport':'agy_cli','cli_model':'saved-summary-high','cli_effort':'high'}}},self.secrets)
        self.assertEqual(saved['function_providers']['sentence']['model'],'saved-sentence')
        self.assertEqual(saved['function_providers']['review'],review)
        self.assertTrue(saved['ai_correction']);self.assertTrue(saved['summary_enabled'])
        self.assertEqual(saved['translation'],'en')
        self.assertEqual(saved['detailed_summary_prompt'],'saved prompt')
        self.assertEqual(self.secrets.get('sentence','https://saved.example/v1'),'dummy-key')
        self.assertNotIn('dummy-key',self.preferences.path.read_text())

    def test_saving_new_mode_migrates_explicit_legacy_choices_losslessly(self):
        self.write({'base_url':'https://saved.example/v1','model':'saved-sentence','summary_enabled':True,
            'review_base_url':'https://review.example/v1','review_model':'saved-review'})
        saved=self.preferences.save({'function_providers':{'summary':{'transport':'agy_cli','cli_model':'saved-high'}}},self.secrets)
        self.assertEqual(saved['function_providers']['sentence']['model'],'saved-sentence')
        self.assertEqual(saved['function_providers']['review']['model'],'saved-review')
        self.assertTrue(saved['summary_enabled'])

    def test_empty_api_model_cannot_replace_a_working_saved_choice(self):
        self.preferences.save({'function_providers':{'sentence':{
            'base_url':'https://saved.example/v1','model':'working-sentence'}}},self.secrets)
        before=self.preferences.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'sentence AI 模型不能为空'):
            self.preferences.save({'function_providers':{'sentence':{
                'base_url':'https://saved.example/v1','model':''}}},self.secrets)
        self.assertEqual(self.preferences.path.read_bytes(),before)


if __name__=='__main__':unittest.main()
