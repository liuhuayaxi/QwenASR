"""An account-wide cap shared by all AI functions and models in one service."""
import asyncio
import hashlib
import os
import weakref
from .ai_secrets import endpoint_identity

_gates = weakref.WeakKeyDictionary()


def service_concurrency():
    try:
        return max(1, min(32, int(os.getenv('QWEN_ASR_AI_TOTAL_CONCURRENCY', '4'))))
    except ValueError:
        return 4


def account_gate(base_url, key):
    gates = _gates.setdefault(asyncio.get_running_loop(), {})
    identity = (endpoint_identity(base_url), hashlib.sha256(key.encode()).hexdigest())
    return gates.setdefault(identity, asyncio.Semaphore(service_concurrency()))
