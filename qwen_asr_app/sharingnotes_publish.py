"""Incremental Sharing Notes publishing through the v1 publish API.

Only notes whose published bytes changed are sent: each upsert is built
locally, fingerprinted, and skipped when it equals the last accepted one.
Missing image blobs reported by the API are registered and uploaded, then the
upsert is replayed. Contract: sharingnotes-code ``contract/README.md``.

Configuration (environment wins over ``<PRIVATE_ROOT>/sharingnotes-publish/config.json``):
``SHARINGNOTES_PUBLISH_URL``, ``SHARINGNOTES_PUBLISH_TOKEN`` and
``SHARINGNOTES_PUBLISH_COURSES`` (comma separated; empty publishes nothing).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .durable import atomic_json
from .paths import PRIVATE_ROOT
from .sharingnotes_export import export_note

CONTRACT_VERSION = 1
STATE_DIR = PRIVATE_ROOT / 'sharingnotes-publish'
NOTES_PATH = '/api/publish/v1/notes'
DELETE_PATH = '/api/publish/v1/notes/delete'
BLOBS_PATH = '/api/publish/v1/blobs'
MAX_REPLAYS = 3


class PublishError(RuntimeError):
    """Transport or protocol failure; the session stays pending."""


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PublishConfig:
    def __init__(self, url='', token='', courses=()):
        self.url = str(url or '').strip().rstrip('/')
        self.token = str(token or '').strip()
        self.courses = [c for c in (str(v).strip() for v in courses) if c]
        if self.url:
            parsed = urlsplit(self.url)
            loopback = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
            if parsed.scheme not in {'https', 'http'} or (parsed.scheme == 'http' and not loopback) \
                    or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('发布接口必须是 HTTPS 地址（本机调试可用 http://127.0.0.1）')

    @property
    def configured(self):
        return bool(self.url and self.token and self.courses)

    @classmethod
    def load(cls, path=None):
        path = Path(path or STATE_DIR / 'config.json')
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            saved = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            saved = {}
        courses = os.environ.get('SHARINGNOTES_PUBLISH_COURSES')
        courses = [c for c in courses.split(',')] if courses is not None else saved.get('courses') or []
        return cls(os.environ.get('SHARINGNOTES_PUBLISH_URL') or saved.get('url'),
                   os.environ.get('SHARINGNOTES_PUBLISH_TOKEN') or saved.get('token'), courses)


class HttpTransport:
    """``request(method, path, body, content_type) -> (status, json)`` over HTTPS."""

    def __init__(self, base_url, token, timeout=60):
        self.base_url, self.token, self.timeout = base_url.rstrip('/'), token, timeout

    def request(self, method, path, body=b'', content_type='application/json'):
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': content_type,
            'Accept': 'application/json', 'User-Agent': 'QwenASR-sharingnotes-publisher/1'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b'{}')
            except ValueError:
                if exc.code in (401, 403):
                    raise PublishError(f'发布令牌无效或无权限（HTTP {exc.code}）') from None
                raise PublishError(f'发布接口返回 HTTP {exc.code}') from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PublishError(f'发布接口不可达：{exc}') from None


def empty_page_links(version):
    return {'version': version, 'sections': [], 'materials': []}


def build_upsert(exported, expected_version=None, materials=None, page_links=None, page_sets=None,
                 pdfs=None, pages=None):
    """The upsert-note message. Courseware (materials, page links, page sets)
    is supplied by the courseware publisher; without it the note carries the
    contract's empty mapping."""
    note = exported['note']
    return {
        'contractVersion': CONTRACT_VERSION, 'operation': 'upsert_note',
        'expectedVersion': expected_version,
        'note': note, 'catalogEntry': exported['catalogEntry'],
        'references': {'images': list(dict.fromkeys(image['sha256'] for image in note['images'])),
                       'pages': list(pages or []), 'pdfs': list(pdfs or [])},
        'materials': list(materials or []),
        'pageLinks': page_links or empty_page_links(note['version']),
        'pageSets': list(page_sets or []),
    }


def fingerprint(message):
    """Content identity of an upsert, independent of its concurrency condition."""
    body = {k: v for k, v in message.items() if k != 'expectedVersion'}
    return _sha(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


class PublishState:
    """``notes[note_id]`` = last accepted version/fingerprint; ``pending`` = session ids."""

    def __init__(self, path=None):
        self.path = Path(path or STATE_DIR / 'state.json')
        self.lock = threading.RLock()
        self.notes, self.sessions, self.pending = {}, {}, []
        self.reload()

    def reload(self):
        """Re-read the file (another publisher may have written it); pending ids merge."""
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        with self.lock:
            self.notes = data.get('notes') if isinstance(data.get('notes'), dict) else {}
            self.sessions = data.get('sessions') if isinstance(data.get('sessions'), dict) else {}
            self.pending = list(dict.fromkeys([*(data.get('pending') or []), *self.pending]))

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_json(self.path, {'notes': self.notes, 'sessions': self.sessions, 'pending': self.pending})

    def mark(self, session_ids):
        with self.lock:
            before = len(self.pending)
            self.pending = list(dict.fromkeys([*self.pending, *session_ids]))
            if len(self.pending) != before:
                self.save()

    def done(self, session_id):
        with self.lock:
            if session_id in self.pending:
                self.pending.remove(session_id)
                self.save()


@contextlib.contextmanager
def _process_lock(path):
    """One publisher at a time per state file (the app and a manual run)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix('.lock'), 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Publisher:
    def __init__(self, transport, courses, state=None, courseware=None):
        self.transport = transport
        self.courses = set(courses)
        self.state = state or PublishState()
        # Optional hook: exported -> dict of build_upsert courseware kwargs plus
        # ``blobs`` {sha256: (kind, content_type, bytes)} for PDFs/pages/manifests.
        self.courseware = courseware

    def _record(self, session_id, **fields):
        with self.state.lock:
            self.state.sessions[session_id] = {**fields, 'at': _now()}
            self.state.save()

    def _call(self, path, message):
        body = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode()
        status, response = self.transport.request('POST', path, body)
        if not isinstance(response, dict) or response.get('contractVersion') != CONTRACT_VERSION:
            raise PublishError(f'发布接口响应无效（HTTP {status}）')
        if status in (401, 403):
            raise PublishError(response.get('error') or f'发布令牌无效或无权限（HTTP {status}）')
        if status >= 500:
            raise PublishError(response.get('error') or f'发布接口返回 HTTP {status}')
        return response

    def _descriptor(self, sha, blobs):
        if sha not in blobs:
            raise PublishError(f'服务端缺少本机没有的资源 {str(sha)[:12]}')
        kind, content_type, payload = blobs[sha]
        return {'contractVersion': CONTRACT_VERSION, 'sha256': sha, 'kind': kind,
                'contentType': content_type, 'size': len(payload)}, payload

    def _put(self, descriptor, payload):
        path = f"{BLOBS_PATH}/{descriptor['kind']}/{descriptor['sha256']}"
        status, response = self.transport.request('PUT', path, payload, descriptor['contentType'])
        if status >= 300 or not isinstance(response, dict) or response.get('status') != 'accepted':
            error = response.get('error') if isinstance(response, dict) else None
            raise PublishError(error or f"上传 {descriptor['kind']} 失败（HTTP {status}）")

    def _ensure_blob(self, sha, blobs):
        """Register a referenced blob; upload its bytes only if the server lacks them."""
        descriptor, payload = self._descriptor(sha, blobs)
        response = self._call(BLOBS_PATH, descriptor)
        if response.get('status') == 'missing_blobs':
            self._put(descriptor, payload)
        elif response.get('status') != 'accepted':
            raise PublishError(response.get('error') or f'登记资源失败：{response.get("status")}')
        return response.get('status') == 'missing_blobs'

    def publish_session(self, session, read_asset=None, force=False):
        """Publish one recording's note if it changed. Never raises for
        content problems: they are recorded and returned as a status."""
        session_id = str(session.get('id') or '')
        if str(session.get('category') or '') not in self.courses:
            return {'session': session_id, 'status': 'skipped_course'}
        try:
            exported = export_note(session, read_asset)
        except ValueError as exc:
            self._record(session_id, status='invalid', error=str(exc)[:400])
            return {'session': session_id, 'status': 'invalid', 'error': str(exc)}
        if exported is None:
            return {'session': session_id, 'status': 'skipped_unpublishable'}
        note = exported['note']
        blobs = {sha: ('image', 'image/png', data) for sha, data in exported['images'].items()}
        extra = dict(self.courseware(exported) or {}) if self.courseware else {}
        blobs.update(extra.pop('blobs', {}) or {})
        previous = self.state.notes.get(note['id']) or {}
        message = build_upsert(exported, previous.get('version'), **extra)
        stamp = fingerprint(message)
        if not force and previous.get('fingerprint') == stamp:
            return {'session': session_id, 'id': note['id'], 'status': 'unchanged'}
        uploaded = 0
        for sha in blobs:
            uploaded += self._ensure_blob(sha, blobs)
        for _ in range(MAX_REPLAYS + 1):
            response = self._call(NOTES_PATH, message)
            status = response.get('status')
            if status == 'missing_blobs':
                for item in response.get('missingBlobs') or []:
                    descriptor, payload = self._descriptor(item.get('sha256'), blobs)
                    if any(item.get(k) != v for k, v in descriptor.items()):
                        raise PublishError(f"服务端登记的 {descriptor['kind']} 资源描述与本机字节不一致")
                    self._put(descriptor, payload)
                    uploaded += 1
                continue
            break
        else:
            raise PublishError('上传缺失资源后服务端仍报告缺失')
        if status == 'accepted' and response.get('acceptedVersion') == note['version']:
            with self.state.lock:
                self.state.notes[note['id']] = {'session': session_id, 'version': note['version'],
                                                'fingerprint': stamp, 'file': exported['catalogEntry']['file'],
                                                'publishedAt': _now()}
            self._record(session_id, status='accepted', id=note['id'], version=note['version'])
            return {'session': session_id, 'id': note['id'], 'status': 'accepted', 'version': note['version'],
                    'replaced': previous.get('version'), 'uploadedBlobs': uploaded}
        # conflict/rejected: someone else changed the note, or the payload is
        # invalid. Not retried automatically; a person decides.
        error = str(response.get('error') or status)[:400]
        self._record(session_id, status=status or 'invalid_response', id=note['id'], error=error)
        return {'session': session_id, 'id': note['id'], 'status': status, 'error': error}

    def delete_note(self, note_id, expected_version):
        """Explicit removal only: a session missing locally is never deleted."""
        self.state.reload()
        response = self._call(DELETE_PATH, {'contractVersion': CONTRACT_VERSION, 'operation': 'delete_note',
                                            'id': note_id, 'expectedVersion': expected_version})
        if response.get('status') == 'deleted':
            with self.state.lock:
                self.state.notes.pop(note_id, None)
                self.state.save()
        return response

    def sync(self, sessions, read_asset=None, force=False):
        """Publish ``(session_id, loader)`` pairs; transport failures keep ids pending."""
        with _process_lock(self.state.path):
            self.state.reload()
            return self._sync(sessions, read_asset, force)

    def _sync(self, sessions, read_asset, force):
        results = []
        for session_id, load in sessions:
            try:
                session = load()
            except Exception as exc:  # noqa: BLE001 - a broken recording must not stop the queue
                results.append({'session': session_id, 'status': 'unreadable', 'error': str(exc)[:400]})
                self.state.done(session_id)
                continue
            if session is None:
                results.append({'session': session_id, 'status': 'missing'})
                self.state.done(session_id)
                continue
            try:
                results.append(self.publish_session(session, read_asset, force=force))
            except PublishError as exc:
                results.append({'session': session_id, 'status': 'retry', 'error': str(exc)[:400]})
                continue
            except Exception as exc:  # noqa: BLE001 - one bad note must not block the queue
                self._record(session_id, status='error', error=str(exc)[:400])
                results.append({'session': session_id, 'status': 'error', 'error': str(exc)[:400]})
            self.state.done(session_id)
        return results


def store_loader(store, session_id):
    def load():
        record = store.find(session_id)
        return None if record is None else record.public()
    return load


def session_ids_for_paths(paths):
    """Recording ids touched by changed paths (the ``session.json`` beside them)."""
    found = []
    for raw in paths or ():
        path = Path(raw)
        for candidate in (path, path / 'session.json', path.parent / 'session.json'):
            if candidate.name == 'session.json' and candidate.is_file():
                try:
                    ident = json.loads(candidate.read_text(encoding='utf-8')).get('id')
                except (OSError, ValueError, AttributeError):
                    break
                if ident:
                    found.append(str(ident))
                break
    return list(dict.fromkeys(found))


class AutoPublisher:
    """Publishes changed notes in the background while the server runs.

    Library change notifications mark recordings pending (persisted, so a
    restart resumes); a full reconcile runs at start and every
    ``reconcile_interval`` seconds, which costs no network for unchanged notes.
    """

    def __init__(self, store, config=None, transport=None, state=None, debounce=5, retry=60,
                 reconcile_interval=6 * 3600):
        self.store, self.config = store, config
        self.transport, self.state = transport, state
        self.debounce, self.retry, self.reconcile_interval = debounce, retry, reconcile_interval
        self.task = None
        self.publisher = None
        self.last = {}
        self.full = False

    def mark(self, paths=None):
        if self.publisher is None:
            return
        if paths is None:
            self.full = True
            return
        ids = session_ids_for_paths(paths)
        if ids:
            self.publisher.state.mark(ids)

    def _all_ids(self):
        return [record.id for record in self.store.iter_records()]

    def run_once(self):
        ids = list(self.publisher.state.pending)
        if self.full:
            self.full = False
            ids = list(dict.fromkeys([*ids, *self._all_ids()]))
        results = self.publisher.sync([(i, store_loader(self.store, i)) for i in ids])
        changed = [r for r in results if r['status'] not in {'unchanged', 'skipped_course', 'skipped_unpublishable'}]
        self.last = {'at': _now(), 'checked': len(results), 'results': changed[-50:]}
        return results

    async def start(self):
        from .background_wakeup import library_changes
        config = self.config or PublishConfig.load()
        if not config.configured or self.task:
            return False
        self.publisher = Publisher(self.transport or HttpTransport(config.url, config.token),
                                   config.courses, self.state or PublishState())
        library_changes.callbacks.add(self.mark)
        self.full = True
        self.task = asyncio.create_task(self.run())
        return True

    async def run(self):
        from .background_wakeup import library_changes
        next_reconcile = time.monotonic() + self.reconcile_interval
        while True:
            version = library_changes.version
            if time.monotonic() >= next_reconcile:
                self.full = True
                next_reconcile = time.monotonic() + self.reconcile_interval
            if self.full or self.publisher.state.pending:
                try:
                    results = await asyncio.to_thread(self.run_once)
                except Exception as exc:  # noqa: BLE001 - keep the server healthy
                    results = [{'status': 'retry', 'error': str(exc)[:400]}]
                    self.last = {'at': _now(), 'error': str(exc)[:400]}
                if any(r['status'] == 'retry' for r in results):
                    await asyncio.sleep(self.retry)
                    continue
            await library_changes.wait(version, max(0, next_reconcile - time.monotonic()))
            await asyncio.sleep(self.debounce)  # coalesce saves of one edit

    async def close(self):
        from .background_wakeup import library_changes
        library_changes.callbacks.discard(self.mark)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
