"""Durable AI preferences; API keys remain in the endpoint-bound secret store."""
import copy
import json
import os

from .ai_secrets import PROVIDER_MODES, endpoint_identity
from .durable import atomic_json
from .paths import PRIVATE_ROOT

PROVIDER_FIELDS = {'base_url', 'model', 'api_mode', 'context_window', 'max_output_tokens',
                   'concurrency', 'extra_params', 'system_prompt', 'transport',
                   'cli_path', 'cli_model', 'cli_effort', 'cli_timeout_seconds', 'cli_proxy_url'}
SETTINGS_FIELDS = {'base_url', 'model', 'context_window', 'summary_interval', 'summary_enabled',
                   'extra_params', 'ai_correction', 'api_mode', 'history_chars', 'translation',
                   'title_prompt', 'detailed_summary_prompt', 'abstract_prompt',
                   'review_base_url', 'review_model', 'review_api_mode', 'review_context_window',
                   'review_extra_params', 'summary_base_url', 'summary_model', 'summary_api_mode',
                   'summary_context_window', 'summary_extra_params', 'classroom_chat_context','note_review_enabled','note_ai_latex_enabled'}


def _saved_providers(settings):
    """Read explicitly saved legacy fields without borrowing another model.

    The old settings page stored sentence fields at the document root and
    review/summary fields with a prefix. A newer partial function map must not
    make those same saved choices disappear. An explicit canonical entry is
    authoritative, including an invalid entry that needs to be fixed.
    """
    providers = settings.get('function_providers', {})
    if not isinstance(providers, dict):
        return providers
    providers = copy.deepcopy(providers)
    for mode in PROVIDER_MODES:
        if mode in providers:
            continue
        prefix = '' if mode == 'sentence' else mode + '_'
        legacy = {field: copy.deepcopy(settings[prefix + field]) for field in PROVIDER_FIELDS
                  if prefix + field in settings}
        if legacy.get('base_url') or legacy.get('model') or legacy.get('cli_model'):
            providers[mode] = legacy
    return providers


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
        providers = _saved_providers(value)
        if providers:
            value['function_providers'] = providers
        return value

    def save(self, settings, secrets):
        if not isinstance(settings, dict) or not isinstance(settings.get('function_providers'), dict):
            raise ValueError('AI 配置格式无效')
        if len(json.dumps(settings, ensure_ascii=False)) > 512_000:
            raise ValueError('AI 配置过大，未保存')
        if 'note_review_enabled' in settings and type(settings['note_review_enabled']) is not bool:
            raise ValueError('笔记审核开关必须是布尔值')
        if 'note_ai_latex_enabled' in settings and type(settings['note_ai_latex_enabled']) is not bool:
            raise ValueError('AI LaTeX 排版开关必须是布尔值')
        previous = self.read()
        # A function-specific settings save updates only the submitted modes.
        # DELETE /api/ai/settings remains the explicit way to clear all choices.
        clean = {key: copy.deepcopy(value) for key, value in previous.items() if key in SETTINGS_FIELDS}
        clean.update({key: copy.deepcopy(value) for key, value in settings.items() if key in SETTINGS_FIELDS})
        previous_providers = previous.get('function_providers', {})
        if not isinstance(previous_providers, dict):
            raise ValueError('本机 AI 功能配置格式无效，原设置已保留')
        if any(mode in PROVIDER_MODES and not isinstance(provider,dict)
               and mode not in settings['function_providers'] for mode,provider in previous_providers.items()):
            raise ValueError('本机 AI 功能配置格式无效，原设置已保留')
        providers = {mode: {key: copy.deepcopy(value) for key, value in provider.items() if key in PROVIDER_FIELDS}
                     for mode, provider in previous_providers.items()
                     if mode in PROVIDER_MODES and isinstance(provider, dict)}
        credentials = {}
        for mode, provider in settings['function_providers'].items():
            if mode not in PROVIDER_MODES or not isinstance(provider, dict):
                raise ValueError('AI 功能配置无效')
            transport = str(provider.get('transport') or 'api').strip().lower()
            if 'cli_proxy_url' not in provider and providers.get(mode, {}).get('cli_proxy_url'):
                provider = {**provider, 'cli_proxy_url':providers[mode]['cli_proxy_url']}
            if transport not in {'api', 'agy_cli'}:
                raise ValueError(f'{mode} AI 整理通道无效')
            if transport == 'api':
                endpoint_identity(provider.get('base_url'))
                if not str(provider.get('model') or '').strip():
                    raise ValueError(f'{mode} AI 模型不能为空，原配置已保留')
            elif not str(provider.get('cli_model') or provider.get('model') or '').strip():
                raise ValueError(f'{mode} AGY CLI 模型不能为空')
            providers[mode] = {key: value for key, value in provider.items() if key in PROVIDER_FIELDS}
            if transport == 'api' and provider.get('api_key'):
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

    def task_config(self, config=None, *, course=None, required_modes=(), preserve_flags=(), operation=None):
        """Replace all caller AI settings with the durable local configuration.

        Providers are deliberately never recovered from request data, browser
        storage, archived batch options, or environment defaults. Keys are
        resolved separately for the saved endpoint by AISecretStore.
        """
        result = copy.deepcopy(config if isinstance(config, dict) else {})
        try:
            saved = self.read()
        except (OSError, ValueError) as exc:
            if required_modes:
                raise
            # A damaged optional AI configuration must not disable capture/ASR.
            saved = {}
            result['_ai_config_warning'] = 'AI 配置无法读取，已关闭本次 AI 功能；录音仍可保存'
        providers = saved.get('function_providers', {})
        if not isinstance(providers, dict):
            if required_modes:
                raise ValueError('本机 AI 配置格式无效，请重新保存设置')
            providers, saved = {}, {}
            result['_ai_config_warning'] = 'AI 配置结构无效，已关闭本次 AI 功能'
        if not providers:
            if os.getenv('QWEN_ASR_TESTING') == '1':
                return copy.deepcopy(config if isinstance(config, dict) else {})
        preserved = set(preserve_flags)
        for key, default in (('ai_correction', False), ('summary_enabled', False), ('translation', 'off')):
            if key not in preserved or key not in result:
                result[key] = saved.get(key, default)
        required_modes = set(required_modes)
        if operation == 'live':
            if result.get('ai_correction'):
                required_modes.update(('sentence', 'review'))
            if result.get('translation', 'off') != 'off':
                required_modes.add('sentence')
            if result.get('summary_enabled'):
                required_modes.add('review')
        elif operation == 'segment' and (result.get('ai_correction') or result.get('translation', 'off') != 'off'):
            required_modes.add('correction' if result.get('_batch_segment_mode') else 'sentence')
        ai = {}
        for mode in PROVIDER_MODES:
            raw = providers.get(mode, {})
            if not isinstance(raw, dict):
                if mode in required_modes:
                    raise ValueError(f'本机 {mode} AI 配置格式无效')
                raw = {}
            provider = {key: copy.deepcopy(value) for key, value in raw.items() if key in PROVIDER_FIELDS}
            if mode in required_modes:
                transport = str(provider.get('transport') or 'api').strip().lower()
                if transport not in {'api', 'agy_cli'}:
                    raise ValueError(f'本机 {mode} AI 通道无效')
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
        try:
            ai['history_chars'] = int(saved.get('history_chars', 2000))
        except (TypeError, ValueError):
            ai['history_chars'] = 2000
        result['ai'] = ai
        result['_ai_settings_source'] = 'local_saved'
        result['note_review_enabled'] = saved.get('note_review_enabled') is True
        result['note_ai_latex_enabled'] = saved.get('note_ai_latex_enabled') is True
        # These are global settings, not per-request overrides. Only task
        # controls (e.g. "generate a summary now") may enable an operation.
        # Normal browser task actions use the durable global switches. Local
        # server bridges (for example SmartClass course replay) may pass an
        # explicit operation flag that was selected and persisted by that
        # bridge; preserve only the named flags, never provider or credential
        # fields.
        try:
            result['summary_interval_minutes'] = max(1, int(saved.get('summary_interval') or 10))
        except (TypeError, ValueError):
            result['summary_interval_minutes'] = 10
        if course is not None:
            result['terminology'] = copy.deepcopy(profile.get('terminology') or [])
        for key in ('_note_summary_system_prompt', '_note_abstract_system_prompt', '_title_prompt'):
            result.pop(key, None)
        summary_provider = providers.get('summary')
        result['_note_summary_system_prompt'] = (summary_provider.get('system_prompt') if isinstance(summary_provider, dict) else '') or saved.get('detailed_summary_prompt', '')
        result['_note_abstract_system_prompt'] = saved.get('abstract_prompt', '')
        result['_title_prompt'] = saved.get('title_prompt', '')
        return result


ai_preferences = AIPreferences()
