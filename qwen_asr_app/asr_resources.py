"""Task-owned ASR placement and bounded, read-only host utilization samples."""
from __future__ import annotations

import asyncio
import math
import plistlib
import subprocess
import sys
import time


def percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 1) if math.isfinite(value) and 0 <= value <= 100 else None


def execution(record):
    """Use this recording's execution evidence, never the current global mode."""
    meta = record.metadata
    stream = meta.get('course_stream') or {}
    config = (stream.get('inference_settings') or meta.get('inference_settings')
              or (meta.get('batch_options') or {}).get('inference_settings') or {})
    active = stream.get('asr_execution') or {}
    if active.get('provider') in {'local', 'remote'}:
        provider, basis = active['provider'], active.get('state', 'observed')
        fallback = bool(active.get('fallback'))
    else:
        observed = next((segment.get('metrics') or {} for segment in reversed(record.segments)
                         if (segment.get('metrics') or {}).get('provider') in {'local', 'remote'}), {})
        provider = observed.get('provider')
        basis, fallback = 'last_segment', bool(observed.get('fallback'))
        if not provider:
            mode = config.get('asr_provider')
            provider = {'local': 'local', 'remote_only': 'remote'}.get(mode)
            basis = 'configured' if provider else 'unknown'
    name = {'local': 'Mac', 'remote': 'Windows'}.get(provider)
    label = f'{name} 转写' if name else '转写设备待确认'
    if fallback:
        label += '（已回退）'
    return {'provider': provider, 'device': name, 'label': label, 'basis': basis,
            'fallback': fallback, 'updated_at': active.get('updated_at', 0)}, config


def mac_usage():
    result = {'device': 'Mac', 'scope': 'host', 'cpu_percent': None, 'gpu_percent': None,
              'sampled_at': time.time()}
    if sys.platform != 'darwin':
        return result
    try:
        import psutil
        result['cpu_percent'] = percent(psutil.cpu_percent(interval=.15))
    except (ImportError, OSError):
        pass
    try:
        raw = subprocess.check_output(['/usr/sbin/ioreg', '-a', '-r', '-c', 'AGXAccelerator', '-d', '1'],
                                      stderr=subprocess.DEVNULL, timeout=2)
        devices = plistlib.loads(raw)
        values = [percent((item.get('PerformanceStatistics') or {}).get('Device Utilization %'))
                  for item in devices]
        values = [value for value in values if value is not None]
        if values:
            result['gpu_percent'] = max(values)
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
        pass
    result['sampled_at'] = time.time()
    return result


def windows_usage(config):
    from .inference_providers import remote_client
    result = {'device': 'Windows', 'scope': 'host', 'cpu_percent': None, 'gpu_percent': None,
              'sampled_at': time.time()}
    try:
        with remote_client(config).session() as session:
            response = session.get('/v1/resources/usage', timeout=3)
            response.raise_for_status()
            data = response.json()['data']
        # Reject WSL-only CPU measurements and mismatched/unversioned payloads.
        if data.get('device') == 'Windows' and data.get('scope') == 'host':
            result.update(cpu_percent=percent(data.get('cpu_percent')),
                          gpu_percent=percent(data.get('gpu_percent')),
                          sampled_at=data.get('sampled_at'))
    except Exception:
        pass
    return result


class ResourceSampler:
    """Sample only on visible-page requests, sharing one probe per host / 5 s."""
    def __init__(self):
        self.cache = {}
        self.pending = {}

    async def sample(self, provider, config):
        key = (provider, str(config.get('base_url', '')) if provider == 'remote' else '')
        cached = self.cache.get(key)
        if cached is None or time.monotonic() - cached[0] >= 5:
            if key not in self.pending:
                async def observe():
                    try:
                        value = await asyncio.to_thread(windows_usage, config) if provider == 'remote' else await asyncio.to_thread(mac_usage)
                        self.cache[key] = (time.monotonic(), value)
                    finally:
                        self.pending.pop(key, None)
                self.pending[key] = asyncio.create_task(observe())
            # Closing a tab must not release ownership of an in-flight probe.
            await asyncio.shield(self.pending[key])
            cached = self.cache[key]
        value = dict(cached[1])
        sampled = value.get('sampled_at')
        # A timestamp from a remote host may differ slightly from this clock.
        fresh = isinstance(sampled, (int, float)) and not isinstance(sampled, bool) and -5 <= time.time() - sampled <= 15
        if not fresh:
            value.update(cpu_percent=None, gpu_percent=None)
        value['sample_age_seconds'] = round(time.monotonic() - cached[0], 1)
        return value


sampler = ResourceSampler()


async def course_resources(entries, store):
    result = {}
    for entry in entries:
        ident = entry.get('record_id') or entry.get('recording_id')
        record = store.find(ident) if store and ident else None
        if record is None:
            continue
        placement, config = execution(record)
        phase = (entry.get('phases') or {}).get('asr') or {}
        running = entry.get('state') == 'running' and phase.get('state') == 'running'
        stream = record.metadata.get('course_stream') or {}
        complete = record.metadata.get('status') == 'completed' or (
            stream.get('input_complete') and stream.get('chunks')
            and stream.get('completed_chunks', 0) >= len(stream['chunks']))
        usage = await sampler.sample(placement['provider'], config) if running and not complete and placement['provider'] else None
        result[entry['id']] = {**placement, 'usage': usage}
    return result
