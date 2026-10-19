#!/usr/bin/env python3
"""Read a running local workbench's CPU/RSS counters without starting a model."""
import argparse
import json
import time
from urllib.error import URLError
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--interval', type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.seconds <= 600 or not 1 <= args.interval <= 60:
        parser.error('port, seconds or interval is outside the supported range')
    started = time.monotonic()
    samples = []
    while True:
        try:
            with urlopen(f'http://127.0.0.1:{args.port}/api/health', timeout=5) as response:
                health = json.load(response)
        except (URLError, OSError, ValueError) as exc:
            parser.exit(1, f'Cannot read the running local workbench: {exc}\n')
        if 'cpu_seconds' not in health:
            parser.exit(1, 'This service does not expose CPU counters; restart it with the updated code.\n')
        memory = health.get('memory') or {}
        elapsed = time.monotonic() - started
        sample = {'elapsed_seconds': round(elapsed, 3), 'pid': health['pid'],
                  'cpu_seconds': health['cpu_seconds'], 'rss_bytes': memory.get('rss_bytes'),
                  'asr_worker_rss_bytes': memory.get('asr_worker_rss_bytes'),
                  'local_model_loaded': health.get('local_model_loaded'),
                  'asr_worker_pid': health.get('asr_worker_pid')}
        samples.append(sample)
        if elapsed >= args.seconds:
            break
        time.sleep(min(args.interval, args.seconds - elapsed))
    first, last = samples[0], samples[-1]
    duration = last['elapsed_seconds'] - first['elapsed_seconds']
    same_process = all(sample['pid'] == first['pid'] for sample in samples)
    average = ((last['cpu_seconds'] - first['cpu_seconds']) / duration * 100
               if same_process and duration > 0 else None)
    print(json.dumps({'scope': 'workbench process CPU; workbench and ASR-worker RSS shown separately',
                      'average_workbench_cpu_percent': round(average, 3) if average is not None else None,
                      'service_restarted_during_sample': not same_process,
                      'samples': samples}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
