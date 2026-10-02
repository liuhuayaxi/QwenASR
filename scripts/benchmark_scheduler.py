"""Synthetic queue benchmark only: this does not measure actual ASR speed."""
import asyncio
import json
import statistics
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qwen_asr_app.scheduler import PriorityGate


async def scenario(priority):
    gate = PriorityGate(max_overtakes=8)
    waits = []
    async def work(kind, duration):
        start = time.monotonic()
        async with gate.slot(0 if priority and kind == 'live' else 10):
            if kind == 'live':
                waits.append(time.monotonic() - start)
            await asyncio.sleep(duration)
    async with gate.slot():
        tasks = [asyncio.create_task(work('background', .03)) for _ in range(3)]
        tasks += [asyncio.create_task(work('live', .003)) for _ in range(3)]
        await asyncio.sleep(.01)  # Current GPU operation cannot be preempted.
    await asyncio.gather(*tasks)
    return waits


async def main():
    results = {'type': 'synthetic_queue_only', 'real_ASR_speedup': None}
    for name, priority in [('FIFO', False), ('realtime_priority', True)]:
        values = []
        for _ in range(10):
            values += await scenario(priority)
        results[name] = {'count': len(values), 'p50_wait_ms': round(statistics.median(values)*1000, 2),
                         'p95_wait_ms': round(sorted(values)[int(len(values)*.95)-1]*1000, 2)}
    target = Path(__file__).resolve().parent.parent / 'output' / 'phase1-scheduler-benchmark.json'
    target.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
