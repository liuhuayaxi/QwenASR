#!/usr/bin/env python3
"""Detached 闪念贝壳 re-push queue.

Pushes recordings one by one through the running QwenASR server API (so it
shares the server's single 闪念贝壳 session): text + images only
(include_audio=false), a fresh single-memo note, old note retired to 「待删除」.
Resumable: finished ids are skipped, interrupted pushes resume without a second
memo. When 闪念贝壳 is slow or unreachable it backs off and retries instead of
grinding. Afterwards it syncs sharingnotes and runs the Google Drive backup.

    nohup .venv/bin/python scripts/ideashell_push_queue.py ID... &
    cat local-data/ideashell-push-queue/status.json
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / 'local-data' / 'ideashell-push-queue'
STATUS = STATE_DIR / 'status.json'
LOG = STATE_DIR / 'queue.log'
API = 'http://127.0.0.1:8765'
SERVER = 'https://mcp.ideashell.cn/mcp'
BACKOFF = [120, 300, 600, 900, 1800]  # seconds; caps at 30 min
MAX_ATTEMPTS = 12


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def log(message):
    line = f'{now()} {message}'
    print(line, flush=True)
    with LOG.open('a') as handle:
        handle.write(line + '\n')


def load():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {'items': {}}


def save(state):
    state['updated_at'] = now()
    state['pid'] = os.getpid()
    tmp = STATUS.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(STATUS)


def call(method, path, body=None, timeout=1800, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(API + path, data=data, method=method,
                                     headers={'content-type': 'application/json', **(headers or {})})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors='replace')
        try:
            detail = json.loads(text).get('detail', text)
        except ValueError:
            detail = text
        return exc.code, {'detail': str(detail)[:500]}, time.monotonic() - started
    except Exception as exc:  # timeouts, server restarts
        return 0, {'detail': f'{type(exc).__name__}: {exc}'[:500]}, time.monotonic() - started


def wait_connected(state):
    """Block until the server reports a live 闪念贝壳 session."""
    for attempt in range(10_000):
        code, body, _ = call('GET', f'/api/ideashell/status?server_url={SERVER}', timeout=30)
        if code == 200 and body.get('connected'):
            return
        if code == 200 and body.get('authorization_url'):
            state['blocked'] = '闪念贝壳授权已失效，需要在工作台重新授权'
            save(state)
        if code == 200 and body.get('status') in {'saved', 'error'}:
            call('POST', '/api/ideashell/connect', {'server_url': SERVER,
                 'callback_url': API + '/api/ideashell/oauth/callback'}, timeout=60)
        delay = min(60 * (attempt + 1), 600)
        state['waiting'] = f'等待闪念贝壳连接（{body.get("status") or body.get("detail")}），{delay}s 后重试'
        save(state)
        time.sleep(delay)
    raise SystemExit('闪念贝壳一直无法连接')


def sync_of(rid):
    code, body, _ = call('GET', f'/api/recordings/{rid}', timeout=60)
    return (body.get('ideashell_sync') or {}) if code == 200 else {}


def summarize(detail):
    """Derive memo / attachment counts from note_detail without assuming its exact shape."""
    counts = {}
    def walk(value, key=''):
        if isinstance(value, dict):
            for name, item in value.items():
                walk(item, name.lower())
        elif isinstance(value, list):
            for marker, label in (('memo', 'memos'), ('version', 'memos'), ('image', 'images'),
                                  ('pic', 'images'), ('doc', 'documents'), ('file', 'documents'),
                                  ('pdf', 'documents'), ('audio', 'audio'), ('record', 'audio'),
                                  ('voice', 'audio')):
                if marker in key:
                    counts[label] = max(counts.get(label, 0), len(value))
                    break
            for item in value:
                walk(item, key)
    walk(detail)
    return counts


def verify(item, rid, retire=True):
    code, body, seconds = call('POST', f'/api/recordings/{rid}/ideashell/verify', {'retire': retire}, timeout=1200)
    item['verify_seconds'] = round(seconds, 1)
    if code != 200:
        item['verify_error'] = body.get('detail')
        return False
    detail = body.get('detail') or {}
    counts = summarize(detail)
    item.update(new_note_id=body['note_id'], in_course_folder=body['in_course_folder'],
                old_still_in_course_folder=body['old_still_in_course_folder'],
                retired_now=body['retired_now'], readback=counts,
                readback_keys=sorted(detail)[:30] if isinstance(detail, dict) else [])
    item.pop('verify_error', None)
    # memos: 1 when the API exposes memo lists; absent means a single body.
    ok = (body['in_course_folder'] and not body['old_still_in_course_folder']
          and counts.get('memos', 1) == 1 and counts.get('documents', 0) == 0
          and counts.get('audio', 0) == 0)
    return ok


def push_one(state, rid):
    item = state['items'].setdefault(rid, {'state': 'pending', 'attempts': 0})
    if item.get('state') == 'done':
        return
    if item.get('state') not in {'pushed', 'verifying', 'pushed_unverified'}:
        cutoff = state.get('pushed_after') or state['started_at']
        sync = sync_of(rid)
        # A push that finished while we were not watching (crash, restart)
        # must not be repeated: a second push would publish another note.
        if sync.get('status') == 'synced' and str(sync.get('last_pushed_at') or '') >= cutoff:
            item.update(state='pushed', new_note_id=sync.get('note_id'), pushed_at=sync.get('last_pushed_at'))
            log(f'{rid} already pushed at {sync.get("last_pushed_at")}, verifying only')
    if item.get('state') in {'pushed', 'verifying', 'pushed_unverified'}:
        attempts = 0
    else:
        attempts = MAX_ATTEMPTS
    for _ in range(attempts):
        wait_connected(state)
        state.pop('waiting', None)
        item.update(state='pushing', started_at=item.get('started_at') or now())
        item['attempts'] += 1
        save(state)
        code, preview, seconds = call('POST', f'/api/recordings/{rid}/ideashell/preview',
                                      {'content_mode': 'combined', 'include_audio': False}, timeout=600)
        if code == 200:
            body = {'content_mode': 'combined', 'preview_digest': preview['digest'],
                    'assets_digest': preview['assets_digest'], 'audio_sha256s': [], 'include_audio': False}
            code, result, seconds = call('POST', f'/api/recordings/{rid}/ideashell/push', body)
        else:
            result = preview
        item['last_call_seconds'] = round(seconds, 1)
        if code == 200:
            item.update(images_attached=result.get('images_attached'), documents_attached=result.get('documents_attached'),
                        audio_attached=result.get('audio_attached'), push_retired=result.get('retired_note_ids'),
                        new_note_id=result.get('note_id'), pushed_at=now())
            log(f'{rid} pushed note={result.get("note_id")} images={result.get("images_attached")} '
                f'docs={result.get("documents_attached")} audio={result.get("audio_attached")} in {seconds:.0f}s')
            break
        detail = str(result.get('detail'))
        item['last_error'] = detail
        log(f'{rid} attempt {item["attempts"]} failed ({code}) after {seconds:.0f}s: {detail[:200]}')
        if code == 409 and '未知' in detail:
            item['state'] = 'needs_manual'  # unknown create: never blindly re-create
            save(state)
            return
        if code == 400 and '重新预览' not in detail and '连接' not in detail:
            item['state'] = 'failed'
            save(state)
            return
        delay = BACKOFF[min(item['attempts'] - 1, len(BACKOFF) - 1)]
        item['state'] = f'retry in {delay}s'
        save(state)
        time.sleep(delay)
    else:
        if attempts:
            item['state'] = 'failed'
            save(state)
            return
    item['state'] = 'verifying'
    save(state)
    for attempt in range(4):
        if verify(item, rid):
            item['state'] = 'done'
            break
        log(f'{rid} verify not clean: {item.get("verify_error") or item.get("readback")} '
            f'old_in_folder={item.get("old_still_in_course_folder")}')
        time.sleep(BACKOFF[attempt])
    else:
        item['state'] = 'pushed_unverified'
    item['finished_at'] = now()
    save(state)


def finish(state):
    sharing = ROOT.parent / 'sharingnotes'
    state['sharingnotes'] = 'running'
    save(state)
    try:
        subprocess.run(['git', 'pull', '--rebase', '-q'], cwd=sharing, check=True, timeout=600)
        run = subprocess.run([sys.executable, 'scripts/sync_site.py'], cwd=sharing, capture_output=True,
                             text=True, timeout=7200)
        tail = (run.stdout + run.stderr).strip().splitlines()[-1:] or ['']
        state['sharingnotes'] = {'exit': run.returncode, 'result': tail[0][:400]}
    except Exception as exc:
        state['sharingnotes'] = {'error': str(exc)[:400]}
    log(f'sharingnotes: {state["sharingnotes"]}')
    save(state)
    headers = {'X-QwenASR-Client': 'drive-backup', 'Origin': API}
    for _ in range(120):
        code, body, _ = call('POST', '/api/library/drive/start', {}, timeout=60, headers=headers)
        if code == 200:
            break
        time.sleep(30)  # another drive job is running
    while True:
        code, body, _ = call('GET', '/api/library/drive', timeout=60)
        job = (body or {}).get('job') or {}
        if code == 200 and not body.get('busy') and job.get('status') not in {'scanning', 'running', 'uploading'}:
            break
        time.sleep(30)
    state['drive'] = {key: job.get(key) for key in ('status', 'total', 'uploaded', 'skipped', 'failed', 'errors')}
    log(f'drive: {state["drive"]}')
    save(state)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load()
    ids = [value for value in sys.argv[1:] if re.fullmatch(r'[\w-]+', value)]
    state['queue'] = ids or state.get('queue', [])
    state.update(started_at=state.get('started_at') or now(), phase='pushing')
    # Pushes done after this moment (this batch) count as finished.
    state.setdefault('pushed_after', os.environ.get('PUSHED_AFTER') or state['started_at'])
    save(state)
    log(f'queue start pid={os.getpid()} ids={len(state["queue"])}')
    for rid in state['queue']:
        state['current'] = rid
        save(state)
        push_one(state, rid)
    state.update(current=None, phase='syncing')
    save(state)
    finish(state)
    state['phase'] = 'finished'
    save(state)
    log('queue finished')


if __name__ == '__main__':
    main()
