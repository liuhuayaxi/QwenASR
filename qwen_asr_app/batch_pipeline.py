from __future__ import annotations

import asyncio
import time
from typing import Any

from .batch_ai import correct_batch_segment, correct_batch_segments
from .ai import concurrency_for, context_window_for, uses_agy_gemini, agy_rewrite_source_budget, retry_transient_ai
from .context_budget import estimate_tokens

CORRECTION_BATCH_SECONDS = 1800


class SegmentCorrectionPipeline:
    """Bounded concurrent AI consumers overlap the next GPU transcription.

    Corrections persist by segment id, so completion order cannot change the
    recording order. Preceding transcript is read-only reference context.
    """

    def __init__(self, record: Any, config: dict[str, Any]) -> None:
        self.record, self.config = record, config
        if record is not None:
            from .refinement import restore_checkpoints
            restore_checkpoints(record)
        self.concurrency = concurrency_for(config, "correction")
        self.grouped = bool(config.get("ai_correction")) and str(config.get("translation", "off")) == "off" and uses_agy_gemini(config, "correction")
        self.batch_seconds = CORRECTION_BATCH_SECONDS
        self.batch_tokens = agy_rewrite_source_budget(config,'correction')
        self.buffer: list[dict[str, Any]] = []
        self.buffer_tokens = 0
        self.submit_lock = asyncio.Lock()
        self.seen_ids = set()
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
            job = await self.jobs.get()
            try:
                if job is None:
                    return
                segments = job if isinstance(job, list) else [job]
                started = time.time()
                for segment in segments:
                    timing = segment.setdefault("metrics", {})
                    timing["ai_queue_seconds"] = round(max(0, started - timing.get("ai_queued_at", started)), 3)
                    if self.grouped:
                        timing["ai_batch_segments"] = len(segments)
                if self.grouped:
                    updated_segments = await correct_batch_segments(self.record, segments, self.config)
                else:
                    updated_segments = [await correct_batch_segment(self.record, segments[0], self.config)]
                for updated in updated_segments:
                    updated.setdefault("metrics", {}).update(ai_seconds=round(time.time() - started, 3))
                    if not self.grouped and self.record is not None:
                        self.record.upsert_segment(updated)
                    if updated.get("ai_error"):
                        self.events.put_nowait({"type": "ai_error", "id": updated["id"],
                                               "message": updated["ai_error"], "scope": "segment"})
                    else:
                        self.events.put_nowait({"type": "ai_segment", "segment": updated,
                                               "stage": f"AI 已保存校对段 {updated['id']}" if self.grouped else f"AI 已精修段 {updated['id']} · 本地转写同时进行"})
                if self.grouped and self.record is not None:
                    self.record.save_all()
            finally:
                self.jobs.task_done()

    async def _enqueue(self, value) -> None:
        # Propagate an unexpected consumer failure rather than waiting forever
        # on a full queue with nobody left to drain it.
        pending = asyncio.create_task(self.jobs.put(value))
        try:
            done, _ = await asyncio.wait({pending, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                await self.task
            await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _flush(self) -> None:
        if not self.buffer:
            return
        batch = list(self.buffer)
        await self._enqueue(batch)
        self.buffer.clear()
        self.buffer_tokens = 0

    async def submit(self, segment: dict[str, Any]) -> None:
        async with self.submit_lock:
            if segment is None:
                await self._flush()
                await self._enqueue(None)
                return
            if self.task.done():
                await self.task
            if self.grouped and segment["id"] in self.seen_ids:
                return
            if self.record is not None:
                from .refinement import pending_ids
                existing=next((s for s in self.record.segments if s['id']==segment['id']),None)
                if existing is not None and segment['id'] not in pending_ids(self.record):
                    self.seen_ids.add(segment['id'])
                    return
            segment.setdefault("metrics", {})["ai_queued_at"] = time.time()
            segment["ai_pending"] = True
            if self.record is not None:
                self.record.upsert_segment(segment)
            if not self.grouped:
                await self._enqueue(segment)
                return
            cost = estimate_tokens(str(segment.get("raw") or "")) + 24
            start = float(segment.get("start", 0))
            end = float(segment.get("end", start))
            if self.buffer:
                first_start = float(self.buffer[0].get("start", start))
                previous_end = float(self.buffer[-1].get("end", start))
                if (self.buffer_tokens + cost > self.batch_tokens
                        or end - first_start > self.batch_seconds
                        or start < previous_end):
                    await self._flush()
            self.seen_ids.add(segment["id"])
            self.buffer.append(segment)
            self.buffer_tokens += cost
            if self.buffer_tokens >= self.batch_tokens or end - float(self.buffer[0].get("start", start)) >= self.batch_seconds:
                await self._flush()

    async def close(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def finish(self) -> None:
        for _ in range(self.concurrency):
            await self.submit(None)
        await self.task
        from .refinement import finalize_corrections
        finalize_corrections(self.record)
