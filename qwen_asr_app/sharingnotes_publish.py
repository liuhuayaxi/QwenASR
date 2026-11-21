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


def state_path(url):
    """One checkpoint file per publish server: versions from one server mean nothing to another."""
    return STATE_DIR / f'state-{_sha(str(url or "").rstrip("/").encode())[:16]}.json'


class PublishState:
    """``notes[note_id]`` = last accepted version/fingerprint (or a deletion
    marker); ``pending`` = session ids awaiting publication.

    Notes and sessions are written only while a publisher holds the process
    lock. Pending ids live in their own file under a short lock so a save
    notification never writes stale checkpoints; each mark gets a fresh token
    so a save during publication keeps the id pending.
    """

    def __init__(self, path=None):
        self.path = Path(path or STATE_DIR / 'state.json')
        self.pending_path = self.path.with_name(self.path.stem + '.pending.json')
        self.lock = threading.RLock()
        self.notes, self.sessions, self.marks = {}, {}, {}
        self.blobs = set()  # "kind/sha256" the server confirmed it stores
        self.reload()

    @property
    def pending(self):
        return list(self.marks)

    def _read_marks(self):
        try:
            data = json.loads(self.pending_path.read_text(encoding='utf-8')).get('pending')
        except (OSError, ValueError, AttributeError):
            data = None
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _write_marks(self, marks):
        self.pending_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(self.pending_path, {'pending': marks})
        self.marks = marks

    def reload(self):
        """Re-read the files (another publisher may have written them)."""
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        with self.lock:
            self.notes = data.get('notes') if isinstance(data.get('notes'), dict) else {}
            self.sessions = data.get('sessions') if isinstance(data.get('sessions'), dict) else {}
            self.blobs = set(data.get('blobs') or []) if isinstance(data.get('blobs'), list) else set()
            with _process_lock(self.pending_path):
                self.marks = self._read_marks()

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_json(self.path, {'notes': self.notes, 'sessions': self.sessions, 'blobs': sorted(self.blobs)})

    def mark(self, session_ids):
        with self.lock, _process_lock(self.pending_path):
            marks = self._read_marks()
            for ident in session_ids:
                marks.pop(str(ident), None)
                marks[str(ident)] = os.urandom(8).hex()
            self._write_marks(marks)

    def token(self, session_id):
        with self.lock:
            return self.marks.get(str(session_id))

    def done(self, session_id, token=None):
        """Clear a pending id unless it was marked again after ``token`` was read."""
        with self.lock, _process_lock(self.pending_path):
            marks = self._read_marks()
            current = marks.get(str(session_id))
            if current is not None and current == token:
                del marks[str(session_id)]
                self._write_marks(marks)
            else:
                self.marks = marks


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
        size = payload.stat().st_size if isinstance(payload, Path) else len(payload)
        return {'contractVersion': CONTRACT_VERSION, 'sha256': sha, 'kind': kind,
                'contentType': content_type, 'size': size}, payload

    def _put(self, descriptor, payload):
        path = f"{BLOBS_PATH}/{descriptor['kind']}/{descriptor['sha256']}"
        if isinstance(payload, Path):  # courseware is read only when the server lacks it
            payload = payload.read_bytes()
            if hashlib.sha256(payload).hexdigest() != descriptor['sha256']:
                raise PublishError(f"{descriptor['kind']} 文件在发布时被修改，下次再发布")
        status, response = self.transport.request('PUT', path, payload, descriptor['contentType'])
        if status >= 300 or not isinstance(response, dict) or response.get('status') != 'accepted':
            error = response.get('error') if isinstance(response, dict) else None
            raise PublishError(error or f"上传 {descriptor['kind']} 失败（HTTP {status}）")
        with self.state.lock:
            self.state.blobs.add(f"{descriptor['kind']}/{descriptor['sha256']}")

    def _ensure_blob(self, sha, blobs):
        """Register a referenced blob; upload its bytes only if the server lacks them."""
        descriptor, payload = self._descriptor(sha, blobs)
        if f"{descriptor['kind']}/{sha}" in self.state.blobs:
            return None  # confirmed earlier; skipped
        response = self._call(BLOBS_PATH, descriptor)
        if response.get('status') == 'missing_blobs':
            self._put(descriptor, payload)
        elif response.get('status') != 'accepted':
            raise PublishError(response.get('error') or f'登记资源失败：{response.get("status")}')
        with self.state.lock:
            self.state.blobs.add(f"{descriptor['kind']}/{sha}")
        return response.get('status') == 'missing_blobs'

    def _dependents(self, note_id, materials):
        """Sessions of other published notes that list one of these materials with an older PDF."""
        current = {m['id']: m['sha256'] for m in materials}
        found = []
        for other, entry in self.state.notes.items():
            if other == note_id or entry.get('deleted') or not entry.get('session'):
                continue
            if any(mid in current and sha != current[mid] for mid, sha in (entry.get('materials') or {}).items()):
                found.append(entry['session'])
        return found

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
        try:
            extra = dict(self.courseware(exported, session) or {}) if self.courseware else {}
        except ValueError as exc:
            self._record(session_id, status='invalid', error=str(exc)[:400])
            return {'session': session_id, 'status': 'invalid', 'error': str(exc)}
        except OSError as exc:
            raise PublishError(f'读取课件失败：{exc}') from None
        blobs.update(extra.pop('blobs', {}) or {})
        previous = self.state.notes.get(note['id']) or {}
        if previous.get('deleted') and not force:
            return {'session': session_id, 'id': note['id'], 'status': 'skipped_deleted'}
        message = build_upsert(exported, previous.get('version'), **extra)
        stamp = fingerprint(message)
        if not force and previous.get('fingerprint') == stamp:
            return {'session': session_id, 'id': note['id'], 'status': 'unchanged'}
        uploaded, cached = 0, []
        for sha in blobs:
            result = self._ensure_blob(sha, blobs)
            if result is None:
                cached.append(sha)
            uploaded += bool(result)
        for _ in range(MAX_REPLAYS + 1):
            response = self._call(NOTES_PATH, message)
            status = response.get('status')
            if status == 'rejected' and cached:
                # The server forgot blobs we skipped registering (e.g. a reset): register them now.
                with self.state.lock:
                    self.state.blobs.difference_update(f'{blobs[sha][0]}/{sha}' for sha in cached)
                uploaded += sum(bool(self._ensure_blob(sha, blobs)) for sha in cached)
                cached = []
                continue
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
                                                'materials': {m['id']: m['sha256'] for m in message['materials']},
                                                'publishedAt': _now()}
                # A shared material's PDF changed: notes still mapped to the old PDF follow it.
                requeue = self._dependents(note['id'], message['materials'])
            if requeue:
                self.state.mark(requeue)
            self._record(session_id, status='accepted', id=note['id'], version=note['version'])
            return {'session': session_id, 'id': note['id'], 'status': 'accepted', 'version': note['version'],
                    'replaced': previous.get('version'), 'uploadedBlobs': uploaded, 'requeued': requeue}
        # conflict/rejected: someone else changed the note, or the payload is
        # invalid. Not retried automatically; a person decides.
        error = str(response.get('error') or status)[:400]
        self._record(session_id, status=status or 'invalid_response', id=note['id'], error=error)
        return {'session': session_id, 'id': note['id'], 'status': status, 'error': error}

    def delete_note(self, note_id, expected_version):
        """Explicit removal only: a session missing locally is never deleted."""
        with _process_lock(self.state.path):
            self.state.reload()
            response = self._call(DELETE_PATH, {'contractVersion': CONTRACT_VERSION, 'operation': 'delete_note',
                                                'id': note_id, 'expectedVersion': expected_version})
            if response.get('status') == 'deleted':
                # Keep a marker so reconciliation does not republish it; --force does.
                with self.state.lock:
                    self.state.notes[note_id] = {'deleted': True, 'version': None, 'deletedAt': _now()}
                    self.state.save()
        return response

    def sync(self, sessions, read_asset=None, force=False, loader=None):
        """Publish ``(session_id, loader)`` pairs; transport failures keep ids pending.
        With ``loader(session_id)``, notes requeued because a shared PDF changed
        are published in the same run."""
        with _process_lock(self.state.path):
            self.state.reload()
            results = self._sync(sessions, read_asset, force)
            done = {str(ident) for ident, _ in sessions}
            for _ in range(MAX_REPLAYS if loader else 0):
                follow = [i for r in results for i in r.get('requeued') or () if i not in done]
                follow = list(dict.fromkeys(follow))
                if not follow:
                    break
                done.update(follow)
                results += self._sync([(i, loader(i)) for i in follow], read_asset, False)
            return results

    def _sync(self, sessions, read_asset, force):
        results = []
        for session_id, load in sessions:
            token = self.state.token(session_id)
            try:
                session = load()
            except Exception as exc:  # noqa: BLE001 - a broken recording must not stop the queue
                results.append({'session': session_id, 'status': 'unreadable', 'error': str(exc)[:400]})
                self.state.done(session_id, token)
                continue
            if session is None:
                results.append({'session': session_id, 'status': 'missing'})
                self.state.done(session_id, token)
                continue
            try:
                results.append(self.publish_session(session, read_asset, force=force))
            except PublishError as exc:
                results.append({'session': session_id, 'status': 'retry', 'error': str(exc)[:400]})
                if token is None:  # reached by a full reconcile: keep it for the retry
                    self.state.mark([session_id])
                continue
            except Exception as exc:  # noqa: BLE001 - one bad note must not block the queue
                self._record(session_id, status='error', error=str(exc)[:400])
                results.append({'session': session_id, 'status': 'error', 'error': str(exc)[:400]})
            self.state.done(session_id, token)
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
                 reconcile_interval=6 * 3600, courseware=None):
        self.store, self.config, self.courseware = store, config, courseware
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
        results = self.publisher.sync([(i, store_loader(self.store, i)) for i in ids],
                                      loader=lambda i: store_loader(self.store, i))
        changed = [r for r in results if r['status'] not in {'unchanged', 'skipped_course', 'skipped_unpublishable', 'skipped_deleted'}]
        self.last = {'at': _now(), 'checked': len(results), 'results': changed[-50:]}
        return results

    async def start(self):
        from .background_wakeup import library_changes
        config = self.config or PublishConfig.load()
        if not config.configured or self.task:
            return False
        self.publisher = Publisher(self.transport or HttpTransport(config.url, config.token),
                                   config.courses, self.state or PublishState(state_path(config.url)),
                                   courseware=self.courseware)
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
