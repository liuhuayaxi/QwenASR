#!/usr/bin/env python3
"""Measure idle library/task reads using disposable recordings and no models.

Run with the application's core Python environment. --source can point at a
second checkout to compare the same fixture and workload across revisions.
"""
import argparse
import asyncio
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--records', type=int, default=40)
    parser.add_argument('--segments', type=int, default=300)
    parser.add_argument('--iterations', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.records <= 1000 or not 1 <= args.segments <= 5000 or not 1 <= args.iterations <= 100:
        parser.error('fixture sizes are outside the supported range')
    with tempfile.TemporaryDirectory(prefix='qwenasr-idle-benchmark-') as raw:
        os.environ['QWEN_ASR_DATA_DIR'] = raw
        os.environ['QWEN_ASR_TESTING'] = '1'
        for key in ('QWEN_ASR_STORAGE_ROOT', 'QWEN_ASR_SETTINGS_PATH', 'QWEN_ASR_LEGACY_RECORDINGS_DIR'):
            os.environ.pop(key, None)
        sys.path.insert(0, str(args.source.resolve()))
        import app
        from qwen_asr_app.storage import RecordingRecord
        from qwen_asr_app.runtime_memory import rss_bytes
        root = app.app_settings.storage_root / '未分类'
        for i in range(args.records):
            ident = f'idle-fixture-{i:06d}'
            folder = root / ident
            folder.mkdir(parents=True)
            metadata = {'id': ident, 'kind': 'upload', 'status': 'completed',
                        'title': f'Synthetic lecture {i}', 'category': '未分类',
                        'started_at': '2026-10-01T08:00:00+08:00',
                        'sample_count': args.segments * 30 * 16000,
                        'ai_note_summary': {'summary': 'synthetic saved note\n' * 5000, 'abstract': 'Synthetic abstract'}}
            segments = [{'id': n + 1, 'raw': 'synthetic transcript ' * 25,
                         'start': n * 30, 'end': (n + 1) * 30} for n in range(args.segments)]
            (folder / 'session.json').write_text(json.dumps(metadata))
            (folder / 'transcript.json').write_text(json.dumps(segments))
            (folder / 'classroom-notes.json').write_text(json.dumps({'version': 1, 'recording_id': ident, 'notes': []}))
        del metadata, segments

        async def sample(label):
            gc.collect()
            reads = 0
            original = RecordingRecord.load
            def load(folder):
                nonlocal reads
                reads += 1
                return original(folder)
            tracemalloc.start()
            rss_before = rss_bytes()
            started = time.perf_counter()
            cpu = time.process_time()
            with patch.object(RecordingRecord, 'load', side_effect=load):
                for _ in range(args.iterations):
                    result = await app.recording_library(include_settings=False, limit=20)
                    assert result['total'] == args.records
                    app.compact_ai_tasks()
            elapsed_cpu = time.process_time() - cpu
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return {'phase': label, 'iterations': args.iterations,
                    'full_record_loads': reads, 'cpu_seconds': round(elapsed_cpu, 4),
                    'wall_seconds': round(elapsed, 4), 'python_peak_bytes': peak,
                    'rss_before': rss_before, 'rss_after': rss_bytes()}
        async def run():
            return [await sample('cold'), await sample('warm')]
        print(json.dumps({'records': args.records, 'segments_per_record': args.segments,
                          'models_loaded': False, 'samples': asyncio.run(run())}, indent=2))


if __name__ == '__main__':
    main()
