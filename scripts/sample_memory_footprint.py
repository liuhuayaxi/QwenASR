#!/usr/bin/env python3
"""Read-only macOS idle acceptance: report RSS and physical footprint separately."""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def footprint_bytes(pid: int) -> int:
    text = subprocess.check_output(['vmmap', '-summary', str(pid)], text=True,
                                   stderr=subprocess.DEVNULL, timeout=5)
    match = re.search(r'Physical footprint:\s+([0-9.]+)([KMG])', text)
    if match is None:
        raise RuntimeError(f'No physical footprint returned for PID {pid}')
    return round(float(match[1]) * {'K':1024, 'M':1024**2, 'G':1024**3}[match[2]])


def sample(port: int) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(path):
        with opener.open(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
            return json.load(response)
    health, tasks = get('/api/health'), get('/api/tasks')
    pid = int(health['pid'])
    processes = {}
    for line in subprocess.check_output(['ps','-axo','pid=,ppid='], text=True, timeout=5).splitlines():
        child, parent = map(int, line.split()); processes[child] = parent
    owned = {pid}
    while True:
        expanded = owned | {child for child, parent in processes.items() if parent in owned}
        if expanded == owned:break
        owned = expanded
    sizes, errors = {}, []
    for ident in sorted(owned):
        try:sizes[str(ident)] = footprint_bytes(ident)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            errors.append(f'{ident}: {error}')
    return {'time':time.time(), 'pid':pid,
            'rss_bytes':health.get('memory',{}).get('rss_bytes'),
            'physical_footprint_bytes':sizes.get(str(pid)),
            'process_tree_physical_bytes':sum(sizes.values()) if not errors else None,
            'process_footprints':sizes, 'measurement_errors':errors,
            'active_tasks':tasks.get('counts',{}).get('active'),
            'model_loaded':health.get('local_model_loaded'),
            'asr_worker_pid':health.get('asr_worker_pid')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if sys.platform != 'darwin':parser.error('vmmap measurements require macOS')
    if args.seconds < 0 or args.interval <= 0 or not 1 <= args.port <= 65535:
        parser.error('Use nonnegative seconds, a positive interval and a valid port')
    samples, deadline = [], time.monotonic() + args.seconds
    while True:
        samples.append(sample(args.port))
        if time.monotonic() >= deadline:break
        time.sleep(min(args.interval, max(0, deadline-time.monotonic())))
    idle = [s for s in samples if s['active_tasks']==0 and s['model_loaded'] is False and s['asr_worker_pid'] is None]
    complete = len(idle)==len(samples) and all(s['process_tree_physical_bytes'] is not None for s in idle)
    report = {'units':'bytes; 1 MB = 1,000,000 bytes; vmmap text is rounded', 'samples':samples,
              'all_samples_idle':len(idle)==len(samples),
              'all_idle_process_tree_under_100_mb':complete and all(s['process_tree_physical_bytes']<100_000_000 for s in idle)}
    encoded=json.dumps(report,indent=2)
    if args.output:args.output.write_text(encoded+'\n')
    print(encoded)


if __name__ == '__main__':
    main()
