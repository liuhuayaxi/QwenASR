"""Durable AI preferences; API keys remain in the endpoint-bound secret store."""
import copy
import json
import os

from .ai_secrets import PROVIDER_MODES, endpoint_identity
from .durable import atomic_json
from .paths import PRIVATE_ROOT

PROVIDER_FIELDS = {'base_url', 'model', 'api_mode', 'context_window', 'max_output_tokens',
                   'concurrency', 'extra_params', 'system_prompt', 'transport',
                   'cli_path', 'cli_model', 'cli_effort', 'cli_timeout_seconds'}
SETTINGS_FIELDS = {'base_url', 'model', 'context_window', 'summary_interval', 'summary_enabled',
                   'extra_params', 'ai_correction', 'api_mode', 'history_chars', 'translation',
                   'title_prompt', 'detailed_summary_prompt', 'abstract_prompt',
                   'review_base_url', 'review_model', 'review_api_mode', 'review_context_window',
                   'review_extra_params', 'summary_base_url', 'summary_model', 'summary_api_mode',
                   'summary_context_window', 'summary_extra_params', 'classroom_chat_context'}


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
            transport = str(provider.get('transport') or 'api').strip().lower()
            if transport not in {'api', 'agy_cli'}:
                raise ValueError(f'{mode} AI 整理通道无效')
            if transport == 'api':
                endpoint_identity(provider.get('base_url'))
            elif not str(provider.get('cli_model') or provider.get('model') or '').strip():
                raise ValueError(f'{mode} AGY CLI 模型不能为空')
            providers[mode] = {key: value for key, value in provider.items() if key in PROVIDER_FIELDS}
            if transport == 'api' and provider.get('api_key'):
                credentials[mode] = {'base_url': provider['base_url'], 'api_key': provider['api_key']}
        if not providers:
            raise ValueError('AI 功能配置不能为空')
        # Older settings pages only submit the original five functions. Keep
        # the independently edited classroom chat configuration when they save.
        previous = self.read()
        if 'chat' not in providers and isinstance(previous.get('function_providers', {}).get('chat'), dict):
            providers['chat'] = copy.deepcopy(previous['function_providers']['chat'])
        if 'classroom_chat_context' not in clean and 'classroom_chat_context' in previous:
            clean['classroom_chat_context'] = copy.deepcopy(previous['classroom_chat_context'])
        clean['function_providers'] = providers
        if credentials:
            secrets.update(credentials)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, clean)
        self.path.chmod(0o600)
        return clean

    def clear(self):
        self.path.unlink(missing_ok=True)

    def task_config(self, config=None, *, course=None, required_modes=()):
        """Replace all caller AI settings with the durable local configuration.

        Providers are deliberately never recovered from request data, browser
        storage, archived batch options, or environment defaults. Keys are
        resolved separately for the saved endpoint by AISecretStore.
        """
        result = copy.deepcopy(config if isinstance(config, dict) else {})
        saved = self.read()
        providers = saved.get('function_providers', {})
        if not isinstance(providers, dict) or not providers:
            if os.getenv('QWEN_ASR_TESTING') == '1':
                return copy.deepcopy(config if isinstance(config, dict) else {})
            raise ValueError('本机 AI 配置格式无效，请重新保存设置')
        ai = {}
        for mode in PROVIDER_MODES:
            raw = providers.get(mode, {})
            if not isinstance(raw, dict):
                raise ValueError(f'本机 {mode} AI 配置格式无效')
            provider = {key: copy.deepcopy(value) for key, value in raw.items() if key in PROVIDER_FIELDS}
            if mode in required_modes:
                transport = str(provider.get('transport') or 'api').strip().lower()
                if transport == 'agy_cli':
                    if not str(provider.get('cli_model') or provider.get('model') or '').strip():
                        raise ValueError(f'本机尚未保存 {mode} 的 AGY CLI 模型，请在设置中保存后重试')
                elif not provider.get('base_url') or not provider.get('model'):
                    raise ValueError(f'本机尚未保存 {mode} 的接口和模型，请在设置中保存后重试')
                if transport != 'agy_cli':
                    endpoint_identity(provider['base_url'])
                params = provider.get('extra_params', {})
                if isinstance(params, str):
                    params = json.loads(params)
                if not isinstance(params, dict):
                    raise ValueError(f'本机 {mode} AI 参数必须是 JSON 对象')
            if mode == 'sentence':
                ai.update(provider)
            else:
                ai[mode + '_provider'] = provider
        profile = course if isinstance(course, dict) else {}
        if profile.get('ai_system_prompt'):
            ai['system_prompt'] = profile['ai_system_prompt']
        ai['history_chars'] = int(saved.get('history_chars', 2000))
        result['ai'] = ai
        result['_ai_settings_source'] = 'local_saved'
        # These are global settings, not per-request overrides. Only task
        # controls (e.g. "generate a summary now") may enable an operation.
        for key, default in (('ai_correction', False), ('summary_enabled', False), ('translation', 'off')):
            result[key] = saved.get(key, default)
        result['summary_interval_minutes'] = max(1, int(saved.get('summary_interval') or 10))
        if course is not None:
            result['terminology'] = copy.deepcopy(profile.get('terminology') or [])
        for key in ('_note_summary_system_prompt', '_note_abstract_system_prompt', '_title_prompt'):
            result.pop(key, None)
        result['_note_summary_system_prompt'] = (providers.get('summary') or {}).get('system_prompt') or saved.get('detailed_summary_prompt', '')
        result['_note_abstract_system_prompt'] = saved.get('abstract_prompt', '')
        result['_title_prompt'] = saved.get('title_prompt', '')
        return result


ai_preferences = AIPreferences()
