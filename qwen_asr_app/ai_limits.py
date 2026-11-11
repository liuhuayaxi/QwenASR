"""An account-wide cap shared by all AI functions and models in one service."""
import asyncio
import hashlib
import os
import weakref
from pathlib import Path
from .ai_secrets import endpoint_identity

_gates = weakref.WeakKeyDictionary()


class ResizableGate:
    """Changing limits never creates a second pool or cancels admitted work.

    A decrease stops new admissions until active work falls below the new cap.
    Waiting cancellation owns no permit; exit releases synchronously.
    """
    def __init__(self, limit):
        self.limit, self.active = limit, 0
        self.changed = asyncio.Event()

    def resize(self, limit):
        self.limit = limit
        self.changed.set()
        return self

    async def __aenter__(self):
        while self.active >= self.limit:
            self.changed.clear()
            await self.changed.wait()
        self.active += 1
        return self

    async def __aexit__(self, *args):
        self.active -= 1
        self.changed.set()


def shared_gate(identity, limit):
    gates = _gates.setdefault(asyncio.get_running_loop(), {})
    return gates.setdefault(identity, ResizableGate(limit)).resize(limit)


def service_concurrency():
    try:
        return max(1, min(32, int(os.getenv('QWEN_ASR_AI_TOTAL_CONCURRENCY', '8'))))
    except ValueError:
        return 8


def account_gate(base_url, key):
    identity = ('account', endpoint_identity(base_url), hashlib.sha256(key.encode()).hexdigest())
    return shared_gate(identity, service_concurrency())


def agy_account_identity():
    # All current AGY calls inherit this service user's CLI authentication.
    # Model, binary spelling and function concurrency cannot split that account.
    home = Path.home().resolve()
    config = Path(os.getenv('XDG_CONFIG_HOME') or home / '.config').expanduser().resolve()
    return ('agy_cli', str(home), str(config))


def agy_account_gate():
    return shared_gate(('account', *agy_account_identity()), service_concurrency())
