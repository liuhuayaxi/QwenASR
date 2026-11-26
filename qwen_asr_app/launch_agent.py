"""Start the macOS workbench in the user's login session.

A detached Popen still inherits the responsible GUI application. In particular,
AGY's Keychain lookup can be denied when BoringNotch owns that process tree.
launchd gives the service its own identity without changing Keychain access.
The plist lives in private state, so it does not enable launch at login.
"""
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time

from .durable import atomic_bytes


_ENVIRONMENT = {
    'HOME', 'USER', 'LOGNAME', 'PATH', 'SHELL', 'TMPDIR', 'LANG', 'TZ',
    'PYTHONMALLOC', 'PYTHONPATH', 'PYTHONUNBUFFERED', 'PYTHONUTF8',
    'MallocAggressiveMadvise', 'MallocLargeCache', 'MallocNanoZone',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
    'SSL_CERT_FILE', 'CURL_CA_BUNDLE', 'HF_HOME', 'HF_HUB_CACHE', 'HF_ENDPOINT',
    'TRANSFORMERS_CACHE', 'MODELSCOPE_CACHE', 'TOKENIZERS_PARALLELISM',
    'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS',
}


def launch_environment(env):
    # Do not persist the caller's editor/session pipes or unrelated API keys.
    return {key: value for key, value in env.items()
            if key in _ENVIRONMENT or key.startswith(('QWEN_ASR_', 'LC_'))}


def _inspect(target):
    result = subprocess.run(['/bin/launchctl', 'print', target],
                            capture_output=True, text=True, timeout=5)
    match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M)
    return result.returncode == 0, int(match[1]) if match else None


class AgentProcess:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return 1
        return None


def start(command, *, root, run, env, port):
    root, run = Path(root).resolve(), Path(run).resolve()
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    label = f'uk.dczxyy.qwenasr.{identity}.{port}'
    domain = f'gui/{os.getuid()}'
    target = f'{domain}/{label}'
    loaded, pid = _inspect(target)
    if pid:
        raise RuntimeError('QwenASR 后台进程仍在运行，请先停止服务再重新启动')
    if loaded:
        subprocess.run(['/bin/launchctl', 'bootout', target], check=True,
                       capture_output=True, timeout=5)
    path = run / 'workbench.plist'
    atomic_bytes(path, plistlib.dumps({
        'Label': label,
        'ProgramArguments': list(command),
        'WorkingDirectory': str(root),
        'EnvironmentVariables': launch_environment(env),
        'StandardOutPath': str(root / 'server.log'),
        'StandardErrorPath': str(root / 'server.log'),
        'RunAtLoad': True,
        'KeepAlive': False,
        'LimitLoadToSessionType': 'Aqua',
    }))
    result = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(path)],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(f'无法在 macOS 登录会话中启动 QwenASR（launchctl 退出码 {result.returncode}）')
    for _ in range(40):
        _, pid = _inspect(target)
        if pid:
            return AgentProcess(pid)
        time.sleep(.1)
    raise RuntimeError('QwenASR 后台进程未能启动，请查看 server.log')
