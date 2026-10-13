"""Read-only timing samples from an existing queue; never submits recognition."""
import argparse
import json
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--seconds', type=int, default=2400)
    parser.add_argument('--completed', type=int, default=3)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    last, baseline, completed = {}, set(), set()
    started = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with args.output.open('w', encoding='utf-8') as out:
        args.output.chmod(0o600)
        while time.monotonic() - started < args.seconds:
            try:
                with opener.open(args.url + '/api/tasks', timeout=10) as response:
                    tasks = json.load(response)['tasks']
                for task in tasks:
                    if task.get('family') != 'mineru':
                        continue
                    inference = task.get('inference') or {}
                    metrics = inference.get('metrics') or {}
                    row = {key: task.get(key) for key in ('id', 'status', 'stage', 'progress',
                           'configured_provider', 'mineru_settings')}
                    row['provider'] = inference.get('provider')
                    row['metrics'] = {key: metrics[key] for key in ('mineru_version', 'tier', 'pages',
                        'vlm_load_seconds', 'parse_seconds_including_load', 'total_seconds',
                        'gateway_job_seconds', 'performance_config', 'vlm_engine',
                        'configured_vlm_engine') if key in metrics}
                    ident = row['id']
                    signature = json.dumps(row, sort_keys=True)
                    if ident not in last and row['status'] == 'completed':
                        baseline.add(ident)
                    if last.get(ident) != signature:
                        last[ident] = signature
                        row['observed_at'] = time.time()
                        out.write(json.dumps(row, ensure_ascii=False) + '\n')
                        out.flush()
                        if row['status'] == 'completed' and ident not in baseline:
                            completed.add(ident)
                            print('Observed completions:', len(completed), flush=True)
                if len(completed) >= args.completed:
                    break
            except (OSError, ValueError, KeyError) as exc:
                print(type(exc).__name__, flush=True)
            time.sleep(1)
    print('Finished observation, completed:', len(completed), flush=True)


if __name__ == '__main__':
    main()
