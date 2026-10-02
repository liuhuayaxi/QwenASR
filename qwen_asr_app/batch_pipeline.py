from __future__ import annotations

import asyncio
import time
from typing import Any

from .batch_ai import correct_batch_segment
from .ai import concurrency_for


class SegmentCorrectionPipeline:
    """Bounded concurrent AI consumers overlap the next GPU transcription.

    Corrections persist by segment id, so completion order cannot change the
    recording order. Preceding transcript is read-only reference context.
    """

    def __init__(self, record: Any, config: dict[str, Any]) -> None:
        self.record, self.config = record, config
        self.concurrency = concurrency_for(config, "correction")
        # Queue contains references to already persisted text, never PCM audio.
        # 512 segments covers a two-hour class even with 15-second ASR blocks.
        self.jobs: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.events: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._workers())

    async def _workers(self) -> None:
        workers = [asyncio.create_task(self._run()) for _ in range(self.concurrency)]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            segment = await self.jobs.get()
            try:
                if segment is None:
                    return
                timing = segment.setdefault("metrics", {})
                started = time.time()
                timing["ai_queue_seconds"] = round(max(0, started - timing.get("ai_queued_at", started)), 3)
                updated = await correct_batch_segment(self.record, segment, self.config)
                updated.setdefault("metrics", {}).update(ai_seconds=round(time.time() - started, 3))
                if self.record is not None:
                    self.record.upsert_segment(updated)
                if updated.get("ai_error"):
                    self.events.put_nowait({"type": "ai_error", "id": updated["id"],
                                           "message": updated["ai_error"], "scope": "segment"})
                else:
                    self.events.put_nowait({"type": "ai_segment", "segment": updated,
                                           "stage": f"AI 已精修段 {updated['id']} · 本地转写同时进行"})
            finally:
                self.jobs.task_done()

    async def submit(self, segment: dict[str, Any]) -> None:
        # Propagate an unexpected consumer failure rather than waiting forever
        # on a full queue with nobody left to drain it.
        if segment is not None:
            segment.setdefault("metrics", {})["ai_queued_at"] = time.time()
            segment["ai_pending"] = True
            if self.record is not None:
                self.record.upsert_segment(segment)
        pending = asyncio.create_task(self.jobs.put(segment))
        try:
            done, _ = await asyncio.wait({pending, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                await self.task
            await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def close(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def finish(self) -> None:
        for _ in range(self.concurrency):
            await self.submit(None)
        await self.task
