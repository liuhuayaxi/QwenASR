"""Independent model settings for classroom intelligence notes enhancement."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ..classroom_chat import validate_provider
from ..durable import atomic_json
from ..paths import PRIVATE_ROOT
from ..ai_secrets import AISecretStore

intelligence_secret_store = AISecretStore(PRIVATE_ROOT / "intelligence-secrets.json")

DEFAULT_INTELLIGENCE_PROMPT = """你是课堂速记整理与智能扩充助手。根据学生在课上记下的速记、当前课堂真实转写与课程参考资料，将速记整理为条理清晰的课堂笔记。
严格区分课堂事实、教材内容与 AI 推理补充：
1. 每一条课堂事实必须严格引用提供的课堂转写依据（引用编号与原话）。
2. 每一条教材事实必须严格引用提供的教材依据（引用编号与原话）。
3. AI 自主推导或补充背景知识必须归入 ai_explanation 并标记需复核（needs_review: true）。
4. 对数字、公式、符号必须进行严格核对，不得在无依据情况下编造或篡改数字。若课堂与教材有差异，需明确指出。
5. 必须返回纯 JSON 对象，不得输出任何 Markdown 标记或多余文字。"""

DEFAULT_INTELLIGENCE_PROVIDER = {
    'transport': 'agy_cli',
    'model': 'gemini-3.8-flash-high',
    'cli_model': 'gemini-3.8-flash-high',
    'cli_effort': 'high',
    'cli_timeout_seconds': 180,
    'context_window': 1_048_576,
    'max_output_tokens': 4096,
    'concurrency': 1,
    'extra_params': {},
    'system_prompt': DEFAULT_INTELLIGENCE_PROMPT,
}


def intelligence_settings_path(custom_path: Path | None = None) -> Path:
    if custom_path is not None:
        return custom_path
    path = PRIVATE_ROOT / 'intelligence-settings.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def get_intelligence_settings(custom_path: Path | None = None, secrets: Any = None) -> dict[str, Any]:
    path = intelligence_settings_path(custom_path)
    if not path.exists():
        return {
            'provider': copy.deepcopy(DEFAULT_INTELLIGENCE_PROVIDER),
            'source': 'default',
            'saved_key': False,
        }

    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise ValueError(f'课堂智能设置文件损坏，已保留原配置：{exc}') from exc

    if not isinstance(data, dict):
        raise ValueError('课堂智能设置格式损坏，已保留原配置')

    raw_provider = data.get('provider')
    if raw_provider is not None and not isinstance(raw_provider, dict):
        raise ValueError('课堂智能模型配置损坏，已保留原配置')

    provider = copy.deepcopy(DEFAULT_INTELLIGENCE_PROVIDER)
    if raw_provider:
        provider.update(raw_provider)
    validated = validate_provider(provider)
    # Ensure api_key is never exposed from disk storage
    validated.pop('api_key', None)

    saved_key = False
    if secrets is not None and hasattr(secrets, 'status'):
        saved_key = bool(secrets.status().get('chat', False))

    return {
        'provider': validated,
        'source': 'saved' if raw_provider else 'default',
        'saved_key': saved_key,
    }


def save_intelligence_settings(
    payload: dict[str, Any],
    custom_path: Path | None = None,
    secrets: Any = None,
) -> dict[str, Any]:
    current = get_intelligence_settings(custom_path, secrets=secrets)
    raw = payload.get('provider') if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        raise ValueError('课堂智能笔记模型配置无效')

    raw_copy = copy.deepcopy(raw)
    if 'model' in raw_copy and 'cli_model' not in raw_copy:
        raw_copy['cli_model'] = raw_copy['model']
    elif 'cli_model' in raw_copy and 'model' not in raw_copy:
        raw_copy['model'] = raw_copy['cli_model']

    api_key = str(raw_copy.pop('api_key', '') or '').strip()
    provider = validate_provider({**current['provider'], **raw_copy})

    # Save credential to secrets store in memory/bound mode; NEVER persist in settings JSON
    if api_key and secrets is not None and hasattr(secrets, 'update'):
        base_url = str(provider.get('base_url') or 'https://api.openai.com/v1')
        secrets.update({'chat': {'base_url': base_url, 'api_key': api_key}})

    # Disk file must NEVER store api_key
    disk_provider = copy.deepcopy(provider)
    disk_provider.pop('api_key', None)

    path = intelligence_settings_path(custom_path)
    doc = {'provider': disk_provider}
    atomic_json(path, doc)
    try:
        path.chmod(0o600)
    except OSError:
        pass

    saved_key = bool(api_key) or (bool(secrets.status().get('chat', False)) if secrets and hasattr(secrets, 'status') else False)
    return {'provider': disk_provider, 'source': 'saved', 'saved_key': saved_key}
