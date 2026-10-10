"""Durable remote operations: uncertainty is never permission to recompute."""
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import httpx

try:
    from .network import validate_url
except ImportError:
    import importlib.util
    spec = importlib.util.spec_from_file_location('_ai_infer_network', Path(__file__).with_name('network.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    validate_url = module.validate_url


class RemoteError(RuntimeError):
    pass


class ProvenNotAccepted(RemoteError):
    proven_not_accepted = True


class RemotePending(RemoteError):
    fallback_unsafe = True


def atomic_receipt(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def fsync_directory(path):
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def digest(path):
    with Path(path).open('rb') as content:
        return hashlib.file_digest(content, 'sha256').hexdigest()


def without_secrets(value):
    if isinstance(value, dict):
        forbidden = {'token', 'api_key', 'authorization', 'password', 'access_token',
                     'refresh_token', 'client_secret', 'private_key', 'ssh_private_key'}
        return {key: without_secrets(item) for key, item in value.items()
                if str(key).lower().replace('-', '_') not in forbidden}
    if isinstance(value, list):
        return [without_secrets(item) for item in value]
    return value


def request_cancel(receipt_path):
    # Separate intent survives a busy/crashed client holding the receipt lock.
    path = Path(receipt_path)
    atomic_receipt(path.with_suffix('.cancel.json'), {'requested_at': time.time()})


def retryable(exc):
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, RemoteError) and str(exc) in {'REMOTE_HTTP_502', 'REMOTE_HTTP_503', 'REMOTE_HTTP_504'})


def safe_extract(archive, destination, maximum=8 * 1024**3):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as bundle:
        total, seen = 0, set()
        for entry in bundle.infolist():
            relative = PurePosixPath(entry.filename)
            if (relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16) or entry.filename in seen
                    or relative.name in {'receipt.json', '.remote-operation.json', 'downloaded-result.zip'}):
                raise RemoteError('UNSAFE_ARCHIVE_PATH')
            seen.add(entry.filename)
            if not (destination / entry.filename).resolve().is_relative_to(destination):
                raise RemoteError('UNSAFE_ARCHIVE_PATH')
            total += entry.file_size
            if total > maximum:
                raise RemoteError('RESULT_TOO_LARGE')
        if bundle.testzip() is not None:
            raise RemoteError('RESULT_ZIP_CORRUPT')
        for entry in bundle.infolist():
            target = destination / entry.filename
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with bundle.open(entry) as inp, target.open('xb') as out:
                    os.fchmod(out.fileno(), 0o600)
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())


class RemoteClient:
    def __init__(self, config):
        # No credential access before the operation has been classified.
        self.cfg = dict(config)
        self.base_url = str(config.get('base_url', '')).rstrip('/')

    def session(self):
        validate_url(self.base_url)
        token = os.environ.get('AI_INFER_TOKEN', '').strip()
        if not token:
            path = Path(self.cfg['token_file']).expanduser()
            if path.stat().st_mode & 0o077:
                raise ValueError('Remote token file must have mode 600')
            token = path.read_text().strip()
        if len(token) < 32:
            raise ValueError('Remote token unavailable')
        return httpx.Client(base_url=self.base_url, headers={'Authorization': 'Bearer ' + token},
            trust_env=False, follow_redirects=False, timeout=httpx.Timeout(
                read=min(30, self.cfg['request_timeout_seconds']), write=min(60, self.cfg['request_timeout_seconds']),
                pool=10, connect=self.cfg['connect_timeout_seconds']))

    @staticmethod
    def data(response):
        if response.status_code >= 400:
            raise RemoteError('REMOTE_HTTP_' + str(response.status_code))
        try:
            value = response.json()
            if not isinstance(value, dict) or value.get('ok') is not True or not isinstance(value.get('data'), dict):
                raise ValueError()
            return value['data']
        except (ValueError, TypeError, KeyError) as exc:
            raise RemoteError('REMOTE_RESPONSE_INVALID') from exc

    def health(self):
        with self.session() as session:
            return self.check_health(session)

    def check_health(self, session):
        value = self.retry_request(lambda: session.get('/v1/health', timeout=10))
        if value.get('status') != 'healthy' or value.get('models_preloaded') is not False:
            raise RemoteError('REMOTE_HEALTH_INVALID')
        return value

    def retry_request(self, operation, stop=None):
        # Used for reads only. An uncertain POST is reconciled, never replayed.
        for attempt in range(3):
            if stop is not None and stop.is_set():
                raise RemotePending('CANCELED')
            try:
                return self.data(operation())
            except Exception as exc:
                if not retryable(exc) or attempt == 2:
                    raise
                time.sleep(.1 * (attempt + 1))

    def run(self, kind, source, output, options=None, progress=None, stop=None, key=None, receipt_path=None):
        path = Path(receipt_path) if receipt_path else Path(output).parent / ('.' + Path(output).name + '-operation') / 'receipt.json'
        legacy = Path(output)/'.remote-operation.json'
        if receipt_path is None and legacy.exists():
            path = legacy
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.with_suffix('.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RemotePending('REMOTE_OPERATION_ALREADY_ACTIVE') from None
            return self._run(kind, source, Path(output), options, progress, stop, key, path)

    def reconcile_cancel(self, receipt_path):
        path = Path(receipt_path)
        request_cancel(path)
        with path.with_suffix('.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'state': 'cancel_unconfirmed'}
            if not path.exists():
                return {'state': 'canceled_before_submission'}
            try:
                saved = json.loads(path.read_text())
                state, ident = saved['state'], saved.get('job_id')
                if state in {'prepared', 'proven_not_accepted', 'local_fallback'} and not ident:
                    saved.update(state='canceled', cancel_requested=True, cancel_resolved=True)
                    atomic_receipt(path, saved)
                    return saved
                if state in {'completed', 'downloaded', 'failed', 'canceled'}:
                    saved.update(cancel_requested=True, cancel_resolved=True)
                    atomic_receipt(path, saved)
                    return saved
                fp = saved['fingerprint']
                self.cfg = {**self.cfg, **fp.get('provider_config', {})}
                self.base_url = fp['endpoint']
                saved.update(cancel_requested=True, state='cancel_unconfirmed')
                atomic_receipt(path, saved)
                with self.session() as session:
                    if not ident:
                        ident = self.data(session.get('/v1/operations/' + saved['key']))['id']
                        saved['job_id'] = ident
                        atomic_receipt(path, saved)
                    result = self.data(session.post('/v1/jobs/' + ident + '/cancel', timeout=5))
                    state = result.get('status')
                    saved.update(state=state if state in {'completed', 'failed', 'canceled'} else 'cancel_requested',
                                 cancel_resolved=state in {'completed', 'failed', 'canceled'})
                    atomic_receipt(path, saved)
                    return saved
            except Exception as exc:
                raise RemotePending('REMOTE_CANCEL_REQUIRES_RECONCILIATION') from exc

    def _read_or_prepare(self, kind, source, options, key, path):
        if path.exists():
            try:
                receipt = json.loads(path.read_text())
                if not receipt['key'] or not isinstance(receipt['fingerprint'], dict) or not receipt['state']:
                    raise ValueError()
                if receipt['fingerprint']['kind'] != kind:
                    raise ValueError()
                return receipt
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise RemotePending('REMOTE_RECEIPT_REQUIRES_REPAIR') from exc
        source = Path(source)
        # Canonical immutable bytes, flushed before a receipt can permit POST.
        blob = path.parent / ('input' + source.suffix.lower())
        temporary = path.parent / ('.input-' + uuid.uuid4().hex)
        try:
            with source.open('rb') as inp, temporary.open('xb') as out:
                os.fchmod(out.fileno(), 0o600)
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            source_sha = digest(temporary)
            os.replace(temporary, blob)
            fsync_directory(blob.parent)
            snapshot = without_secrets(self.cfg)
            receipt = {'schema': 2, 'key': key or uuid.uuid4().hex, 'job_id': None, 'state': 'prepared',
                'blob_path': str(blob), 'fingerprint': {'source_sha256': source_sha, 'kind': kind,
                'options': without_secrets(options or {}), 'endpoint': self.base_url, 'filename': source.name,
                'provider_config': snapshot}, 'created_at': time.time()}
            atomic_receipt(path, receipt)
            return receipt
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self, kind, source, output, options, progress, stop, key, path):
        receipt = self._read_or_prepare(kind, source, options, key, path)
        fp = receipt['fingerprint']
        # Recovery uses the saved endpoint, credential reference and parameters.
        credential_ref = self.cfg.get('token_file')
        self.cfg = dict(fp.get('provider_config') or self.cfg)
        if 'token_file' not in self.cfg and credential_ref:
            self.cfg['token_file'] = credential_ref
        self.base_url = fp['endpoint'].rstrip('/')
        key, ident = receipt['key'], receipt.get('job_id')
        bundle = path.parent / 'downloaded-result.zip'
        intent = path.with_suffix('.cancel.json')
        started = time.monotonic()

        def persist(**values):
            receipt.update(values)
            atomic_receipt(path, receipt)

        def canceled():
            if stop is not None and stop.is_set() and not intent.exists():
                request_cancel(path)
            return not receipt.get('cancel_resolved') and (intent.exists() or receipt.get('cancel_requested') or receipt['state'].startswith('cancel_'))

        def metadata():
            return {'provider': 'remote', 'actual_provider': 'remote', 'fallback': False,
                'job_id': ident, 'operation_key': key, 'config_snapshot': self.cfg,
                'configured_mode': self.cfg.get(kind + '_provider', 'remote_only'),
                'metrics': receipt.get('metrics', {}), 'state': receipt['state']}

        def materialize():
            if not bundle.is_file() or digest(bundle) != receipt.get('bundle_sha256'):
                return False
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix='.materialize-', dir=output.parent) as raw:
                staging = Path(raw) / 'result'
                staging.mkdir(mode=0o700)
                safe_extract(bundle, staging)
                if (staging / 'result.zip').exists():
                    raise RemotePending('REMOTE_BUNDLE_RESERVED_RESULT_PATH')
                shutil.copyfile(bundle, staging / 'result.zip')
                (staging / 'result.zip').chmod(0o600)
                with (staging / 'result.zip').open('rb') as inp:
                    os.fsync(inp.fileno())
                fsync_directory(staging)
                if output.exists():
                    expected = {p.relative_to(staging): digest(p) for p in staging.rglob('*') if p.is_file()}
                    existing = {p.relative_to(output): digest(p) for p in output.rglob('*') if p.is_file()}
                    if expected != existing or output.is_symlink() or any(p.is_symlink() for p in output.rglob('*')):
                        raise RemotePending('REMOTE_EXISTING_RESULT_REQUIRES_REPAIR')
                else:
                    os.replace(staging, output)
                    fsync_directory(output.parent)
            persist(state='downloaded')
            return True

        try:
            if receipt['state'] == 'local_fallback' and canceled():
                persist(state='canceled', cancel_requested=True)
                raise RemotePending('CANCELED_BEFORE_LOCAL_FALLBACK')
            if receipt['state'] == 'local_fallback':
                raise ProvenNotAccepted('REMOTE_PREVIOUSLY_NOT_ACCEPTED')
            if not canceled() and materialize():
                return metadata()
            if receipt['state'] in {'failed', 'canceled', 'expired'}:
                raise RemotePending('REMOTE_TERMINAL_NEW_OPERATION_REQUIRED')
            if canceled() and receipt['state'] in {'prepared', 'proven_not_accepted'} and not ident:
                persist(state='canceled', cancel_requested=True)
                raise RemotePending('CANCELED_BEFORE_SUBMISSION')
            with self.session() as session:
                if not ident and receipt['state'] not in {'prepared', 'proven_not_accepted'}:
                    value = self.data(session.get('/v1/operations/' + key))
                    ident = value.get('id')
                    if not isinstance(ident, str) or not ident:
                        raise RemotePending('REMOTE_LOOKUP_UNCONFIRMED')
                    persist(job_id=ident, state=value.get('status', 'accepted'), metrics=value.get('metrics', {}))
                if canceled():
                    # Intent is durable before any cancellation HTTP call.
                    persist(cancel_requested=True, state='cancel_unconfirmed')
                    if ident:
                        value = self.data(session.post(f'/v1/jobs/{ident}/cancel', timeout=5))
                        persist(state=value['status'] if value.get('status') in {'completed', 'canceled', 'failed'} else 'cancel_requested',
                                cancel_resolved=value.get('status') in {'completed', 'canceled', 'failed'})
                    raise RemotePending('REMOTE_CANCEL_REQUIRES_RECONCILIATION')
                if not ident:
                    self.check_health(session)
                    blob = Path(receipt.get('blob_path') or source)
                    if not blob.is_file():
                        raise RemotePending('REMOTE_IMMUTABLE_INPUT_MISSING')
                    if digest(blob) != fp['source_sha256']:
                        raise RemotePending('REMOTE_IMMUTABLE_INPUT_CHANGED')
                    params = {'kind': kind, 'filename': fp['filename'], 'options': json.dumps(fp['options'], ensure_ascii=False)}
                    def chunks():
                        with blob.open('rb') as content:
                            while chunk := content.read(1024 * 1024):
                                if canceled():
                                    raise RemotePending('CANCELED')
                                yield chunk
                    persist(state='submitting')
                    value = self.data(session.post('/v1/jobs', params=params, content=chunks(), headers={
                        'Idempotency-Key': key, 'Content-Type': 'application/octet-stream',
                        'Content-Length': str(blob.stat().st_size)}))
                    ident = value.get('id')
                    if not isinstance(ident, str) or not ident:
                        raise RemotePending('REMOTE_SUBMIT_UNCONFIRMED')
                    persist(job_id=ident, state='accepted')
                while receipt['state'] not in {'completed', 'downloaded'}:
                    if canceled():
                        persist(cancel_requested=True, state='cancel_unconfirmed')
                        value = self.data(session.post(f'/v1/jobs/{ident}/cancel', timeout=5))
                        persist(state=value['status'] if value.get('status') in {'completed', 'canceled', 'failed'} else 'cancel_requested',
                                cancel_resolved=value.get('status') in {'completed', 'canceled', 'failed'})
                        raise RemotePending('REMOTE_CANCEL_REQUIRES_RECONCILIATION')
                    if time.monotonic() - started > self.cfg['job_timeout_seconds']:
                        raise RemotePending('REMOTE_WAIT_TIMEOUT_RESUME_SAME_OPERATION')
                    value = self.retry_request(lambda: session.get(f'/v1/jobs/{ident}'), stop=stop)
                    persist(state=value['status'], metrics=value.get('metrics', {}))
                    if progress:
                        detail = str(value.get('detail') or '').strip()
                        states = {'queued': '排队', 'loading': '加载模型', 'running': '识别中',
                                  'saving': '保存结果', 'unloading': '释放资源', 'completed': '识别完成'}
                        label = 'Windows · ' + (detail or states.get(receipt['state'], receipt['state']))
                        progress(label, None if kind == 'mineru' else value.get('progress'))
                    if receipt['state'] in {'failed', 'canceled', 'expired'}:
                        raise RemotePending('REMOTE_TERMINAL_NEW_OPERATION_REQUIRED')
                    if receipt['state'] != 'completed':
                        time.sleep(self.cfg['poll_seconds']) if stop is None else stop.wait(self.cfg['poll_seconds'])
                if progress:
                    progress('Windows · 下载识别结果', None)
                part = bundle.with_suffix('.part')
                for attempt in range(3):
                    try:
                        with session.stream('GET', f'/v1/jobs/{ident}/result') as response:
                            if response.status_code != 200:
                                raise RemoteError('REMOTE_HTTP_' + str(response.status_code))
                            size = 0
                            with part.open('wb') as out:
                                os.fchmod(out.fileno(), 0o600)
                                for chunk in response.iter_bytes(1024 * 1024):
                                    if canceled():
                                        raise RemotePending('CANCELED')
                                    size += len(chunk)
                                    if size > 8 * 1024**3:
                                        raise RemoteError('RESULT_TOO_LARGE')
                                    out.write(chunk)
                                out.flush()
                                os.fsync(out.fileno())
                        with zipfile.ZipFile(part) as archive:
                            if archive.testzip() is not None:
                                raise RemoteError('RESULT_ZIP_CORRUPT')
                        os.replace(part, bundle)
                        fsync_directory(bundle.parent)
                        persist(state='completed', bundle_sha256=digest(bundle))
                        break
                    except Exception as exc:
                        part.unlink(missing_ok=True)
                        if attempt == 2 or not (retryable(exc) or isinstance(exc, zipfile.BadZipFile)):
                            raise
                if not materialize():
                    raise RemotePending('REMOTE_BUNDLE_REQUIRES_REPAIR')
                return metadata()
        except ProvenNotAccepted:
            raise
        except Exception as exc:
            if canceled():
                persist(cancel_requested=True, state='cancel_unconfirmed' if receipt['state'] != 'canceled' else 'canceled')
            if isinstance(exc, RemotePending):
                raise
            if (receipt['state'] in {'prepared', 'proven_not_accepted'} and not receipt.get('job_id') and not canceled()
                    and isinstance(exc, (httpx.TransportError, RemoteError))):
                persist(state='proven_not_accepted', last_error=type(exc).__name__)
                raise ProvenNotAccepted('REMOTE_NOT_SUBMITTED_' + type(exc).__name__) from exc
            raise RemotePending('REMOTE_OPERATION_REQUIRES_RECOVERY_' + type(exc).__name__) from exc
