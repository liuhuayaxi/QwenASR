"""Local credentials bound to their exact API endpoint, never to a UI mode alone."""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .paths import PRIVATE_ROOT
from .durable import atomic_json

PROVIDER_MODES = ('sentence', 'review', 'summary', 'note_review', 'correction', 'global', 'chat')


def endpoint_identity(value):
    parsed = urlsplit(str(value or '').strip().rstrip('/'))
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('AI 接口地址必须是无账号、查询参数的 HTTP(S) 地址')
    path = parsed.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    if parsed.port and parsed.port != (443 if parsed.scheme == 'https' else 80):
        host += ':' + str(parsed.port)
    return urlunsplit((parsed.scheme, host, path, '', ''))


class AISecretStore:
    def __init__(self, path=None):
        self.path = path or PRIVATE_ROOT / 'ai-provider-secrets.json'

    def _read(self):
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, mode, base_url=None):
        value = self._read().get(str(mode))
        if not base_url or not isinstance(value, dict):
            return ''
        identity = endpoint_identity(base_url)
        return str(value.get('credentials', {}).get(identity) or (value.get('api_key') if value.get('base_url') == identity else '') or '')

    def status(self):
        saved = self._read()
        return {mode: bool(isinstance(saved.get(mode), dict) and (saved[mode].get('api_key') or saved[mode].get('credentials'))) for mode in PROVIDER_MODES}

    def update(self, values, base_urls=None):
        if not isinstance(values, dict):
            raise ValueError('本机 AI 密钥格式无效')
        saved = self._read()
        for mode in PROVIDER_MODES:
            value = values.get(mode)
            if value is None:
                continue
            address = value.get('base_url') if isinstance(value, dict) else (base_urls or {}).get(mode)
            key = str(value.get('api_key') or '') if isinstance(value, dict) else str(value)
            key = key.strip()
            if not key:
                continue
            if len(key) > 20_000:
                raise ValueError('API Key 长度异常，未保存')
            if not address:
                # Retain older unbound entries for migration, but never send them.
                saved[mode] = key
            else:
                previous = saved.get(mode)
                credentials = dict(previous.get('credentials', {})) if isinstance(previous, dict) else {}
                if isinstance(previous, dict) and previous.get('base_url') and previous.get('api_key'):
                    credentials[previous['base_url']] = previous['api_key']
                credentials[endpoint_identity(address)] = key
                saved[mode] = {'credentials': credentials}
        if saved:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            atomic_json(self.path, saved)
            self.path.chmod(0o600)
        return self.status()

    def clear(self):
        self.path.unlink(missing_ok=True)


ai_secret_store = AISecretStore()
