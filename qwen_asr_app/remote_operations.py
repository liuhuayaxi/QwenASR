"""Private operation snapshots. This module never contacts or imports the node."""
import asyncio
import contextlib
import copy
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from .durable import atomic_json
from .paths import PRIVATE_ROOT

LOCAL = {'mineru_provider': 'local', 'asr_provider': 'local'}


def without_secrets(value):
    if isinstance(value, dict):
        return {k: without_secrets(v) for k, v in value.items()
                if k.lower().replace('-', '_') not in {'token', 'api_key', 'authorization', 'password', 'access_token', 'refresh_token', 'client_secret', 'private_key', 'ssh_private_key'}}
    if isinstance(value, list):
        return [without_secrets(item) for item in value]
    return copy.deepcopy(value)


def saved_config(value=None, *, remote_reference=False):
    if value is None:
        if remote_reference:
            raise ValueError('远程操作缺少配置快照，请修复记录后恢复')
        return dict(LOCAL)
    return copy.deepcopy(value)


def freeze(source, config, parameters, *, parent_id='', kind='mineru', operation_id=None):
    ident = operation_id or uuid.uuid4().hex
    root = PRIVATE_ROOT / 'remote-operations' / ident
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    source = Path(source)
    blob = root / ('source' + source.suffix.lower())
    with source.open('rb') as inp, blob.open('xb') as out:
        os.fchmod(out.fileno(), 0o600)
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
    with blob.open('rb') as inp:
        sha = hashlib.file_digest(inp, 'sha256').hexdigest()
    value = {'schema': 1, 'operation_id': ident, 'parent_id': parent_id, 'kind': kind,
        'source_name': source.name, 'source_path': str(source), 'blob_path': str(blob),
        'source_sha256': sha, 'config': without_secrets(config), 'parameters': without_secrets(parameters),
        'receipt_path': str(root / 'receipt.json'), 'output_dir': str(root / 'output'),
        'contract': {'asr': 'Qwen/Qwen3-ASR-1.7B; BF16; CUDA only; no offload/quantization',
                     'mineru': 'official Flash for native Office; Advanced vLLM BF16 for PDF/images'}}
    atomic_json(root / 'operation.json', value)
    return value


def cancel(path):
    if path:
        atomic_json(Path(path).with_suffix('.cancel.json'), {'requested': True})


def receipt(path):
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict) or not isinstance(value.get('state', ''), str):
            raise ValueError('远程操作收据必须是状态有效的对象')
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {'state': 'receipt_corrupt', 'recovery_required': True}


def discard_completed_payloads(path):
    """After durable output, retain only the small recovery/provenance receipt."""
    if not path:
        return
    path = Path(path)
    allowed = (PRIVATE_ROOT / 'remote-operations', PRIVATE_ROOT / 'uploads', PRIVATE_ROOT / 'tasks')
    root = path.parent
    if root.is_symlink() or not any(root.resolve() != base.resolve() and root.resolve().is_relative_to(base.resolve()) for base in allowed):
        return
    for child in root.iterdir() if root.is_dir() else []:
        if child.name in {'receipt.json', 'classroom-receipt.json', 'operation.json', 'receipt.cancel.json',
                          'receipt.lock', 'classroom-receipt.lock'}:
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


@contextlib.asynccontextmanager
async def execution_lock(lock, config, kind):
    if config.get(kind + '_provider', 'local') == 'local':
        async with lock:
            yield
    else:
        yield


class LocalComputeGuard:
    """Acquire the existing async local lock only around fallback computation."""
    def __init__(self, lock, loop):
        self.lock, self.loop = lock, loop

    def __enter__(self):
        asyncio.run_coroutine_threadsafe(self.lock.acquire(), self.loop).result()

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.lock.release)


@contextlib.contextmanager
def operation_receipt_lock(path):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResultNeedsRepair('同一远程操作仍在运行，请等待现有操作完成') from None
        yield


def mark_local_fallback(path):
    path = Path(path)
    classroom = path.with_name('classroom-receipt.json')
    if path.name == 'receipt.json' and classroom.exists():
        if path.exists():
            raise ResultNeedsRepair('远程操作存在两种协议收据，请先修复记录')
        path = classroom
    with operation_receipt_lock(path):
        value = receipt(path)
        if value.get('state') not in {'proven_not_accepted', 'local_fallback'}:
            raise ValueError('未证实远端没有接受，禁止自动本地重算')
        value['state'] = 'local_fallback'
        atomic_json(path, value)


class ResultNeedsRepair(RuntimeError):
    fallback_unsafe = True


@contextlib.contextmanager
def accepted_result_guard(path):
    try:
        yield
    except Exception as exc:
        state = receipt(path).get('state')
        if state and state not in {'prepared', 'proven_not_accepted', 'local_fallback'} and not getattr(exc, 'fallback_unsafe', False):
            raise ResultNeedsRepair('已有远端操作，保留结果等待恢复或本地构建修复：' + type(exc).__name__) from exc
        raise


def cancel_pending(path):
    saved = receipt(path)
    return bool(path and not saved.get('cancel_resolved') and (
        Path(path).with_suffix('.cancel.json').exists() or saved.get('cancel_requested') or
        str(saved.get('state', '')).startswith('cancel_')))


async def recover_cancellation(path, config):
    from .inference_providers import remote_client
    try:
        return await asyncio.to_thread(remote_client(config).reconcile_cancel, path)
    except Exception:
        return {**receipt(path), 'state': 'cancel_unconfirmed'}


async def download_result(path, config, kind, output):
    saved = receipt(path)
    if saved.get('state') not in {'completed', 'downloaded'}:
        raise ValueError('远程结果尚未完成，不能仅恢复下载')
    from .inference_providers import remote_client
    return await asyncio.to_thread(remote_client(config).run, kind, Path(path).parent/'missing-source',
                                  output, receipt_path=path)
