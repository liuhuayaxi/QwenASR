"""Durable AI preferences; API keys remain in the endpoint-bound secret store."""
import json

from .ai_secrets import PROVIDER_MODES, endpoint_identity
from .durable import atomic_json
from .paths import PRIVATE_ROOT

PROVIDER_FIELDS = {'base_url', 'model', 'api_mode', 'context_window', 'max_output_tokens',
                   'concurrency', 'extra_params', 'system_prompt'}
SETTINGS_FIELDS = {'base_url', 'model', 'context_window', 'summary_interval', 'summary_enabled',
                   'extra_params', 'ai_correction', 'api_mode', 'history_chars', 'translation',
                   'title_prompt', 'detailed_summary_prompt', 'abstract_prompt',
                   'review_base_url', 'review_model', 'review_api_mode', 'review_context_window',
                   'review_extra_params', 'summary_base_url', 'summary_model', 'summary_api_mode',
                   'summary_context_window', 'summary_extra_params'}


class AIPreferences:
    def __init__(self, path=None):
        self.path = path or PRIVATE_ROOT / 'ai-settings.json'

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError('本机 AI 配置格式无效，未用默认值覆盖')
        return value

    def save(self, settings, secrets):
        if not isinstance(settings, dict) or not isinstance(settings.get('function_providers'), dict):
            raise ValueError('AI 配置格式无效')
        if len(json.dumps(settings, ensure_ascii=False)) > 512_000:
            raise ValueError('AI 配置过大，未保存')
        clean = {key: value for key, value in settings.items() if key in SETTINGS_FIELDS}
        providers, credentials = {}, {}
        for mode, provider in settings['function_providers'].items():
            if mode not in PROVIDER_MODES or not isinstance(provider, dict):
                raise ValueError('AI 功能配置无效')
            endpoint_identity(provider.get('base_url'))
            providers[mode] = {key: value for key, value in provider.items() if key in PROVIDER_FIELDS}
            if provider.get('api_key'):
                credentials[mode] = {'base_url': provider['base_url'], 'api_key': provider['api_key']}
        if not providers:
            raise ValueError('AI 功能配置不能为空')
        clean['function_providers'] = providers
        if credentials:
            secrets.update(credentials)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, clean)
        self.path.chmod(0o600)
        return clean

    def clear(self):
        self.path.unlink(missing_ok=True)


ai_preferences = AIPreferences()
