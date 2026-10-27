"""Durable, serialized background job queue with cancellation, coalescing, and crash recovery."""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import uuid
from typing import Any, Callable

from .store import KnowledgeStore


class JobManager:
    def __init__(self, store: KnowledgeStore, sync_fn: Callable[..., dict[str, Any]],
                 defer: Callable[[str], bool] | None = None) -> None:
        self.store = store
        self.sync_fn = sync_fn
        self.defer = defer
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._cancel_tokens: dict[str, threading.Event] = {}
        self._active_job_id: str | None = None
        self._running = False
        self._loop = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    async def start(self) -> None:
        """Start background worker and recover any interrupted jobs into the queue."""
        self._loop = asyncio.get_running_loop()
        self._running = True
        recovered_ids = self.store.recover_interrupted_jobs()
        for jid in recovered_ids:
            self._cancel_tokens[jid] = threading.Event()
            self._queue.put_nowait(jid)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def close(self) -> None:
        """Stop at a batch boundary, then drain the worker before closing SQLite."""
        if not self._running and self._worker_task is None:
            return
        self._running = False
        for event in self._cancel_tokens.values():
            event.set()
        self._queue.put_nowait(None)
        if self._worker_task:
            await self._worker_task
            self._worker_task = None
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)

    def schedule(self, category: str) -> str:
        """Enqueue a background indexing job for category, coalescing if already queued/running."""
        active_job = self.store.get_active_job_for_category(category)
        if active_job:
            return active_job["job_id"]
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        self.store.create_job(job_id, category)
        self._cancel_tokens[job_id] = threading.Event()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, job_id)
        else:
            self._queue.put_nowait(job_id)
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Cancel a running or queued job."""
        job = self.store.get_job(job_id)
        if not job:
            return False

        if job["status"] in ("completed", "failed", "cancelled", "interrupted"):
            return False

        # Set cancellation event
        if job_id in self._cancel_tokens:
            self._cancel_tokens[job_id].set()

        if job["status"] == "queued":
            self.store.update_job(job_id, status="cancelled", message="已取消")

        return True

    def jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_jobs(limit=limit)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get_job(job_id)

    async def _worker_loop(self) -> None:
        while True:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            if job_id is None:
                self._queue.task_done()
                break
            try:
                job = self.store.get_job(job_id)
                if not job or job['status'] == 'cancelled':
                    continue
                if not self._running:
                    self.store.update_job(job_id, status='interrupted', message='服务停止，重启后继续')
                    continue

                self._active_job_id = job_id
                cancel_event = self._cancel_tokens.setdefault(job_id, threading.Event())
                waiting_reported = False
                while self.defer and self._running and not cancel_event.is_set():
                    if not await asyncio.to_thread(self.defer, job['category']):
                        break
                    if not waiting_reported:
                        self.store.update_job(job_id, status='queued', stage='waiting_asr',
                                              message='等待本机转写完成后建立知识索引')
                        waiting_reported = True
                    await asyncio.sleep(.5)
                if not self._running or cancel_event.is_set():
                    self.store.update_job(job_id, status='cancelled' if self._running else 'interrupted',
                                          message='任务已取消' if self._running else '服务停止，重启后继续')
                    continue

                self.store.update_job(job_id, status="running", stage="indexing", message="正在建立课程索引")

                def progress_cb(stage: str, percent: float) -> None:
                    self.store.update_job(
                        job_id,
                        status="running",
                        progress=round(percent, 3),
                        stage=stage,
                        message=stage,
                    )

                def is_stopped() -> bool:
                    return cancel_event.is_set() or not self._running

                loop = asyncio.get_running_loop()
                category = job["category"]

                try:
                    stats = await loop.run_in_executor(
                        self._executor,
                        lambda: self.sync_fn(
                            category=category,
                            stop=is_stopped,
                            progress_cb=progress_cb,
                        ),
                    )

                    if is_stopped():
                        self.store.update_job(job_id, status="cancelled" if self._running else "interrupted",
                                              message="任务已取消" if self._running else "服务停止，重启后继续")
                    else:
                        msg = f"完成: 检查 {stats.get('sources_checked', 0)} 个源，更新 {stats.get('sources_updated', 0)} 个，分块 {stats.get('chunks_indexed', 0)}"
                        if stats.get('waiting_conversion'):
                            msg += f"；{stats['waiting_conversion']} 份资料等待 MinerU 转换，不参与检索"
                        self.store.update_job(
                            job_id,
                            status="completed",
                            progress=1.0,
                            stage="completed",
                            message=msg,
                        )
                except Exception as exc:
                    # ASR may start after an indexing job passed its initial
                    # idle check. Keep the same job queued instead of turning
                    # deliberate GPU priority into a failed index every time.
                    if ('本机模型资源繁忙' in str(exc) and not is_stopped()
                            and self.defer and await asyncio.to_thread(self.defer, category)):
                        self.store.update_job(job_id, status='queued', stage='waiting_asr', error='',
                                              message='等待本机转写完成后继续知识索引')
                        self._queue.put_nowait(job_id)
                        continue
                    self.store.update_job(
                        job_id,
                        status="failed",
                        stage="failed",
                        message=f"索引失败: {exc}",
                        error=str(exc),
                    )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                if job_id:
                    self.store.update_job(
                        job_id,
                        status="failed",
                        error=str(exc),
                        message=f"异常: {exc}",
                    )
            finally:
                self._active_job_id = None
                self._cancel_tokens.pop(job_id, None)
                self._queue.task_done()
