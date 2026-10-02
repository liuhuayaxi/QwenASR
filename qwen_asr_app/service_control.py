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
    try:
        raw = subprocess.check_output(['/usr/sbin/lsof', '-ti', 'tcp:8766'], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return
    for value in raw.splitlines():
        try:
            pid = int(value.strip())
            command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'command='], text=True).strip()
            cwd = subprocess.check_output(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], text=True).strip()
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
        except (OSError, ValueError, subprocess.CalledProcessError):
            continue


def request(name, path, method='GET', token=None):
    # Origin is scheme + host only; the mounted course path is not part of an
    # HTTP origin and would be rejected by the service's request guard.
    headers = {'X-QwenASR-Control': token or '', 'X-QwenASR-Client': 'smartclass', 'Origin': BASE_URL}
    req = urllib.request.Request(ENDPOINTS[name] + path, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=3) as response:
        return json.load(response)


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
            command = [sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(PORT)]
            env = {**os.environ, 'QWEN_ASR_INSTANCE': token}
            with (ROOT / 'server.log').open('ab') as output:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            instances = {name: {'pid': process.pid, 'token': token} for name in ENDPOINTS}
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
        # Freeze new work only when both services are idle; otherwise reopen any
        # previously prepared service. Never signal an unverified/stale PID.
        prepared = []
        try:
            for name in ('courses', 'workbench'):
                current = health(name)
                if not current:
                    continue
                saved = instances.get(name, {})
                if not saved or current.get('instance') != saved.get('token') or current.get('pid') != saved.get('pid'):
                    raise RuntimeError('服务由其他启动器启动，已拒绝停止未知进程')
                request(name, '/api/service/prepare-stop', 'POST', saved['token'])
                prepared.append((name, saved))
        except Exception:
            for name, saved in prepared:
                request(name, '/api/service/cancel-stop', 'POST', saved['token'])
            raise RuntimeError('仍有录音或任务正在进行，请先在页面暂停队列、完成录音及 AI 任务，再停止服务') from None
        killed = set()
        for name, saved in prepared:
            current = health(name)
            if (current and current.get('instance') == saved['token'] and current.get('pid') == saved['pid']
                    and saved['pid'] not in killed):
                os.kill(saved['pid'], signal.SIGTERM)
                killed.add(saved['pid'])
        print('已安全请求停止服务。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
