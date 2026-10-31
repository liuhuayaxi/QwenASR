"""Launch the local QwenASR workbench and its course page as one service."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from .durable import atomic_json
from .paths import PRIVATE_ROOT, ROOT

PORT = int(os.getenv('QWEN_ASR_PORT', '8765'))
BASE_URL = f'http://127.0.0.1:{PORT}'
# The course service is mounted inside app:app.  Keeping both logical
# endpoints here lets status/stop remain backwards compatible with the old
# two-process launcher while only one process owns the port.
ENDPOINTS = {'workbench': BASE_URL, 'courses': BASE_URL + '/course'}


def cleanup_legacy_course_listener():
    """Remove only the old QwenASR standalone 8766 listener.

    Earlier versions launched ``uvicorn app:app`` on 8766 for the course page.
    A process from that version can survive an app update, so clear it once
    before starting the unified 8765 service. The cwd and command checks keep
    unrelated Python services untouched.
    """
    import socket
    probe = socket.socket()
    probe.settimeout(1)
    try:
        if probe.connect_ex(('127.0.0.1', 8766)) != 0:
            return  # nothing listening: skip lsof, which can hang on macOS
    finally:
        probe.close()
    try:
        # lsof can hang indefinitely on macOS; never let it block startup.
        raw = subprocess.check_output(['/usr/sbin/lsof', '-ti', 'tcp:8766'], text=True, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return
    for value in raw.splitlines():
        try:
            pid = int(value.strip())
            command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'command='], text=True).strip()
            cwd = subprocess.check_output(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], text=True, timeout=5).strip()
            if ('uvicorn' in command and '--port 8766' in command
                    and str(ROOT) in cwd):
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        break
                    time.sleep(.1)
        except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue


def request(name, path, method='GET', token=None, timeout=3):
    # Origin is scheme + host only; the mounted course path is not part of an
    # HTTP origin and would be rejected by the service's request guard.
    headers = {'X-QwenASR-Control': token or '', 'X-QwenASR-Client': 'smartclass', 'Origin': BASE_URL}
    req = urllib.request.Request(ENDPOINTS[name] + path, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def process_snapshot():
    """OS evidence remains available even when the HTTP event loop is stuck."""
    output = subprocess.check_output(
        ['/bin/ps', '-axo', 'pid=,ppid=,stat=,lstart=,command='], text=True, timeout=2)
    rows = {}
    for line in output.splitlines():
        fields = line.split(None, 8)
        if len(fields) == 9:
            rows[int(fields[0])] = {'parent': int(fields[1]), 'state': fields[2],
                                   'started': ' '.join(fields[3:8]), 'command': fields[8]}
    return rows


def owned_process(saved, snapshot):
    pid, token = saved.get('pid'), saved.get('token')
    row = snapshot.get(pid)
    if not row or row['state'].startswith('Z'):
        return False
    if not token or (saved.get('started') and saved['started'] != row['started']):
        raise RuntimeError('服务进程标识已变化，已拒绝停止其他程序')
    expected = [f'{sys.executable} -m {module} app:app --host 127.0.0.1 --port {PORT}'
                for module in ('uvicorn', 'qwen_asr_app.server')]
    if not any(row['command'] == prefix or row['command'].startswith(prefix + ' ') for prefix in expected):
        raise RuntimeError('服务进程路径不匹配，已拒绝停止其他程序')
    # Check the launch token without printing environment variables or secrets.
    environment = subprocess.check_output(
        ['/bin/ps', 'eww', '-p', str(pid), '-o', 'command='], text=True, timeout=2)
    if f'QWEN_ASR_INSTANCE={token}' not in environment.split():
        raise RuntimeError('服务启动标识不匹配，已拒绝停止未知进程')
    return True


def terminate_processes(roots, snapshot, grace_seconds=2):
    """Stop owned roots and their children, including detached worker sessions."""
    targets = set(roots)
    while True:
        children = {pid for pid, row in snapshot.items() if row['parent'] in targets}
        if children <= targets:
            break
        targets.update(children)
    def alive():
        current = process_snapshot()
        living = {pid for pid in targets if pid in current
                and current[pid]['started'] == snapshot[pid]['started']
                and not current[pid]['state'].startswith('Z')}
        # Include workers created while the stop request was in flight.
        while True:
            children = {pid for pid, row in current.items() if row['parent'] in living
                        and pid not in targets and not row['state'].startswith('Z')}
            if not children:
                break
            for pid in children:
                snapshot[pid] = current[pid]
            targets.update(children)
            living.update(children)
        return living
    def send(pids, sig):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
    # Signal immediately; neither API readiness nor running work is a veto.
    send(alive(), signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    remaining = alive()
    while remaining and time.monotonic() < deadline:
        time.sleep(.05)
        remaining = alive()
    forced = bool(remaining)
    if remaining:
        send(remaining, signal.SIGKILL)
        deadline = time.monotonic() + 1
        while remaining and time.monotonic() < deadline:
            time.sleep(.05)
            remaining = alive()
    if remaining:
        raise RuntimeError('系统尚未结束服务进程：' + ', '.join(map(str, sorted(remaining))))
    return forced


def stop_services(instances):
    snapshot = process_snapshot()
    owned = {}
    for name, saved in instances.items():
        if name in ENDPOINTS and owned_process(saved, snapshot):
            owned.setdefault(saved['pid'], (name, saved))
    if not owned:
        print('QwenASR 已停止。')
        return
    # Best-effort persistence is bounded and cannot prevent process termination.
    for name, saved in owned.values():
        try:
            request(name, '/api/service/request-stop', 'POST', saved['token'], timeout=.5)
        except (OSError, ValueError):
            pass
    forced = terminate_processes(owned, snapshot)
    # The event loop may have been too stuck to persist /request-stop. Only
    # touch this journal after all owned writers are gone, retaining all items.
    queue_path = PRIVATE_ROOT / 'smartclass' / 'queue.json'
    if queue_path.exists():
        try:
            queue = json.loads(queue_path.read_text())
            queue['paused'] = True
            for item in queue['items']:
                if item.get('state') in {'queued', 'running', 'interrupted'}:
                    item['state'] = 'paused'
            atomic_json(queue_path, queue)
        except (OSError, ValueError, KeyError, TypeError):
            print('服务已退出，但未能更新课程暂停标记；下次启动请检查队列。', file=sys.stderr)
    print('QwenASR 已停止（超时任务已强制结束）。' if forced else 'QwenASR 已停止。')


def health(name):
    try:
        return request(name, '/api/health' if name == 'workbench' else '/api/status')
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'status', 'stop'])
    args = parser.parse_args()
    run = PRIVATE_ROOT / 'services'
    run.mkdir(parents=True, exist_ok=True)
    with (run / 'controller.lock').open('a') as lock:
        # A stuck start/controller must not hold the stop action hostage.
        if args.action != 'stop':
            fcntl.flock(lock, fcntl.LOCK_EX)
        path = run / 'instances.json'
        try:
            instances = json.loads(path.read_text())
        except (OSError, ValueError):
            instances = {}
        if args.action == 'status':
            for name in ENDPOINTS:
                value = health(name)
                print(name + ': ' + ('运行中' if value else '已停止'))
            return
        if args.action == 'start':
            cleanup_legacy_course_listener()
            current = {name: health(name) for name in ENDPOINTS}
            running = [name for name, value in current.items() if value]
            if running:
                # A healthy workbench with a missing course endpoint usually
                # means an older binary is still serving the port.  Refuse to
                # silently mix the old two-process setup with the new one.
                if not all(current[name].get('pid') == current['workbench'].get('pid') for name in running):
                    raise RuntimeError('旧版双服务仍在运行，请先停止 QwenASR 后再启动新版')
                if len(running) == len(ENDPOINTS):
                    print('QwenASR 已在运行。')
                    return
                raise RuntimeError('QwenASR 主服务已运行但课程页面未挂载，请先停止后再启动新版')

            token = uuid.uuid4().hex
            command = [sys.executable, '-m', 'qwen_asr_app.server', 'app:app', '--host', '127.0.0.1', '--port', str(PORT), '--timeout-graceful-shutdown', '10']
            from .launch_memory import service_environment
            env = {**service_environment(), 'QWEN_ASR_INSTANCE': token}
            with (ROOT / 'server.log').open('ab') as output:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            started = process_snapshot().get(process.pid, {}).get('started')
            instances = {name: {'pid': process.pid, 'token': token, 'started': started} for name in ENDPOINTS}
            atomic_json(path, instances)
            for _ in range(90):
                if process.poll() is not None:
                    raise RuntimeError('QwenASR 启动失败，请查看 server.log')
                current = {name: health(name) for name in ENDPOINTS}
                if all(value and value.get('instance') == token and value.get('pid') == process.pid
                       for value in current.values()):
                    break
                time.sleep(.25)
            else:
                raise RuntimeError('QwenASR 启动超时，请查看 server.log')
            print('QwenASR 已就绪（工作台和课程回放共用一个端口）。')
            return
        stop_services(instances)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
