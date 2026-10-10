"""Read-only resource observations; unavailable measurements remain unknown."""
import asyncio
import json
import os
import shutil
from pathlib import Path
import psutil

RUNTIME_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def memory_observation():
    linux = {'state': 'unknown'}
    try:
        wanted = {'MemTotal', 'MemAvailable', 'AnonPages', 'Cached', 'SReclaimable', 'Shmem', 'SwapTotal', 'SwapFree'}
        values = {k: int(v.split()[0])*1024 for k, v in (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines()) if k in wanted}
        linux = {'state': 'observed', 'bytes': values}
    except (OSError, ValueError):
        pass
    cgroup = {'state': 'unknown', 'scope': 'unknown'}
    try:
        entry = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        root = Path('/sys/fs/cgroup')/entry.lstrip('/')
        cgroup = {'state': 'observed', 'path': entry,
                  'scope': 'node_service' if root.name.endswith('.service') else 'shared_or_unverified',
                  'current_bytes': int((root/'memory.current').read_text()),
                  'stat': {key: int(value) for key, value in (line.split() for line in (root/'memory.stat').read_text().splitlines())},
                  'pids': [int(value) for value in (root/'cgroup.procs').read_text().split()]}
    except (OSError, ValueError, StopIteration):
        pass
    return {'linux': linux, 'cgroup': cgroup}


def group_processes(pgid):
    """Only the session created for this node's worker, never other GPU jobs."""
    if not pgid:
        return []
    result = []
    for proc in psutil.process_iter(['pid', 'create_time', 'status']):
        belongs = False
        try:
            belongs = os.getpgid(proc.pid) == pgid
            if not belongs or proc.status() == psutil.STATUS_ZOMBIE:
                continue
            try:
                info = proc.memory_full_info()
                result.append({'pid': proc.pid, 'create_time': proc.create_time(), 'rss_bytes': info.rss,
                               'pss_bytes': getattr(info, 'pss', None), 'state': 'observed'})
            except psutil.Error:
                # A denied RSS/PSS query does not prove the process is absent.
                result.append({'pid': proc.pid, 'rss_bytes': None, 'pss_bytes': None, 'state': 'unknown'})
        except psutil.NoSuchProcess:
            continue
        except (OSError, psutil.Error):
            result.append({'pid': proc.pid, 'rss_bytes': None, 'pss_bytes': None, 'state': 'unknown'})
            continue

    return result


async def host_observation():
    binary = shutil.which('powershell.exe')
    # systemd's narrow PATH intentionally omits Windows executables.
    standard = Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
    if not binary and standard.is_file():
        binary = str(standard)
    if not binary:
        return {'state': 'unknown', 'reason': 'WINDOWS_READ_ONLY_PROBE_UNAVAILABLE'}
    # Fixed read-only command. No shell, no credentials, no arbitrary API input.
    command = "$p=@(Get-Process -Name vmmemWSL -ErrorAction SilentlyContinue); ConvertTo-Json -Compress -InputObject @($p | ForEach-Object { @{pid=$_.Id;WorkingSet64=$_.WorkingSet64;PrivateMemorySize64=$_.PrivateMemorySize64} })"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(binary, '-NoProfile', '-NonInteractive', '-Command', command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(proc.communicate(), 3)
        values = json.loads(output.decode('utf-8-sig'))
        if proc.returncode or not values:
            raise ValueError()
        return {'state': 'observed', 'processes': values if isinstance(values, list) else [values]}
    except (OSError, ValueError, asyncio.TimeoutError):
        return {'state': 'unknown', 'reason': 'WINDOWS_READ_ONLY_PROBE_FAILED'}
    finally:
        if proc is not None and proc.returncode is None:
            with __import__('contextlib').suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def runtime_identity(cfg):
    """Runtime and weight manifest identity without importing model packages."""
    import hashlib
    entries = []
    # An adapter/worker change can change the result with identical weights,
    # packages and config. Include executable source, not just model metadata.
    for folder in ('server', 'workers', 'client'):
        for source in sorted((RUNTIME_SOURCE_ROOT / folder).glob('*.py')):
            entries.append(('source', str(source.relative_to(RUNTIME_SOURCE_ROOT)),
                            hashlib.sha256(source.read_bytes()).hexdigest()))
    for section in ('asr', 'mineru'):
        values = {key: value for key, value in cfg.get(section, {}).items() if key not in ('token', 'token_file')}
        entries.append((section, values))
        binary = Path(values.get('python', '/nonexistent'))
        env = binary.parent.parent
        for metadata in env.glob('lib/python*/site-packages/*.dist-info/METADATA'):
            if metadata.parent.name.split('-')[0] in ('torch', 'transformers', 'mineru', 'vllm'):
                entries.append((str(metadata), hashlib.sha256(metadata.read_bytes()).hexdigest()))
        # Startup and execution verify every weight byte. Health must not read
        # gigabytes of weights and continually repopulate the idle file cache.
        manifest = Path(values.get('manifest_file', '/nonexistent')).expanduser()
        if manifest.is_file():
            entries.append(('manifest', section, hashlib.sha256(manifest.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest()


def source_identity():
    import hashlib
    return {str(path.relative_to(RUNTIME_SOURCE_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ('server', 'workers', 'client')
            for path in sorted((RUNTIME_SOURCE_ROOT / folder).glob('*.py'))}


def descendants(pid):
    result = []
    try:
        for child in psutil.Process(pid).children(recursive=True):
            try:
                result.append({'pid': child.pid, 'create_time': child.create_time()})
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                result.append({'pid': child.pid, 'create_time': None, 'state': 'unknown'})
    except psutil.NoSuchProcess:
        pass
    except psutil.Error:
        result.append({'pid': pid, 'create_time': None, 'state': 'unknown'})
    return result


def tracked_processes(identities):
    live = []
    for identity in identities:
        if identity.get('state') == 'unknown':
            live.append(identity)
            continue
        try:
            proc = psutil.Process(identity['pid'])
            if proc.create_time() == identity['create_time'] and proc.status() != psutil.STATUS_ZOMBIE:
                live.append(identity)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            # A denied identity/status query proves neither absence nor PID
            # ownership. Retain quarantine, but never signal an unverified PID.
            live.append({**identity, 'state': 'unknown'})
    return live


def terminate_tracked(identities, sig):
    # Reuse the exact Process object whose identity was checked. psutil also
    # guards send_signal against PID reuse; never signal an unverified identity.
    for identity in identities:
        if identity.get('state') == 'unknown' or identity.get('create_time') is None:
            raise psutil.AccessDenied(identity['pid'])
        try:
            proc = psutil.Process(identity['pid'])
            if proc.create_time() != identity['create_time'] or proc.status() == psutil.STATUS_ZOMBIE:
                continue
            proc.send_signal(sig)
        except psutil.NoSuchProcess:
            continue
