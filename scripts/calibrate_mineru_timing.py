"""Replay read-only queue observations into the private timing history.

Only complete successful runs enter the mean. A partial run is still included
in the diagnostic summary, clearly flagged as excluded from training.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen_asr_app.inference_providers import inference_settings
from qwen_asr_app.mineru_timing import MineruTiming, TimingHistory, PHASES


def replay(rows, config, history):
    groups = defaultdict(list)
    for row in rows:
        groups[row['id']].append(row)
    summaries = []
    for ident, events in groups.items():
        if len(events) < 2 or events[-1]['status'] != 'completed':
            continue
        now = [events[0]['observed_at']]
        run = MineruTiming(settings=events[0].get('mineru_settings'), config=config,
                           history=history, clock=lambda: now[0], run_id=ident + ':' + str(now[0]),
                           resumed=(events[0]['status'] != 'queued' or any(
                               event['status'] not in {'queued', 'running', 'completed'} for event in events)))
        for event in events:
            now[0] = event['observed_at']
            run.observe(event.get('stage') or '')
        # The final API response is proof of actual completion. Any interval
        # since the last observed stage remains attributed to that stage.
        run.observe('校验与保存')
        run.finish()
        summaries.append({'task': ident, 'trained': run.complete_observation and bool(run.window),
            'pages': run.page_count, 'phase_seconds': {p: round(run.seconds[p], 2) for p in PHASES},
            'metrics': events[-1].get('metrics', {}), 'stage_updates': len(events)})
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('observations', type=Path)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--summary', type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.observations.read_text().splitlines() if line.strip()]
    result = replay(rows, inference_settings.for_task('mineru'), TimingHistory(args.history))
    args.summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    args.summary.chmod(0o600)
    print('Completed observations:', len(result), 'trained:', sum(bool(s['trained']) for s in result))


if __name__ == '__main__':
    main()
