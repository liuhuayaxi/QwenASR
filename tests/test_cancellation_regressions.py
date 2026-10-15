"""Real-thread regressions; model compute and remote transport are test doubles."""
from tests import isolation  # noqa: F401
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from qwen_asr_app.conversion_control import conversion_worker
from qwen_asr_app.inference_providers import RemoteASRProvider
from qwen_asr_app.media_retention import file_worker
from qwen_asr_app.model_runtime import ModelManager


async def wait_for_event(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(.001)
    await asyncio.wait_for(poll(), timeout=5)


async def cancel_repeatedly(task, count=2):
    for _ in range(count):
        task.cancel()
        # Let the cancellation enter the owner's cleanup path before repeating.
        for _ in range(10):
            await asyncio.sleep(0)


def fake_manager():
    manager = ModelManager(Path("/not-a-real-model"))
    manager.worker = SimpleNamespace(is_alive=lambda: True)
    manager.status = "ready"
    manager.load = AsyncMock()
    manager.schedule_unload = Mock()
    return manager


class ModelCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def _inference_case(self, cancellations):
        manager = fake_manager()
        entered, release, second_entered = (threading.Event() for _ in range(3))
        calls = []
        def infer(*args):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release inference")
            else:
                second_entered.set()
            return "text", "Chinese"

        manager._infer_sync = infer
        first = asyncio.create_task(manager._transcribe_local([0.0] * 160, None, ""))
        second = None
        try:
            await wait_for_event(entered)
            await cancel_repeatedly(first, cancellations)
            self.assertFalse(first.done())
            self.assertTrue(manager.infer_lock.locked())
            self.assertTrue(manager.scheduler.busy)
            self.assertEqual(manager.active_jobs, 1)
            second = asyncio.create_task(manager._transcribe_local([0.0] * 160, None, ""))
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(second_entered.is_set())
            self.assertEqual(manager.scheduler.snapshot()["queued"], 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(first, timeout=5)
            result = await asyncio.wait_for(second, timeout=5)
            self.assertEqual(result[:2], ("text", "Chinese"))
            self.assertTrue(second_entered.is_set())
            self.assertFalse(manager.infer_lock.locked())
            self.assertFalse(manager.scheduler.busy)
            self.assertEqual(manager.active_jobs, 0)
        finally:
            release.set()
            await asyncio.gather(*[t for t in (first, second) if t is not None], return_exceptions=True)

    async def test_single_cancel_does_not_overlap_next_inference(self):
        await self._inference_case(1)

    async def test_repeated_cancel_does_not_overlap_next_inference(self):
        await self._inference_case(8)

    async def test_model_close_waits_for_cancelled_inference_thread(self):
        manager = fake_manager()
        entered, release, stopped = (threading.Event() for _ in range(3))
        def infer(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test release timed out")
            return "text", "Chinese"
        manager._infer_sync = infer
        def stop():
            stopped.set()
            manager.worker = None
        manager._stop_worker_sync = stop
        task = asyncio.create_task(manager._transcribe_local([0.0] * 160, None, ""))
        closing = None
        try:
            await wait_for_event(entered)
            await cancel_repeatedly(task)
            closing = asyncio.create_task(manager.close())
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(stopped.is_set(), "model unloaded while inference still used it")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(closing, timeout=5)
            self.assertTrue(stopped.is_set())
            self.assertEqual(manager.status, "idle")
        finally:
            release.set()
            await asyncio.gather(*[t for t in (task, closing) if t is not None], return_exceptions=True)

    async def test_cancelling_queued_request_never_starts_its_worker(self):
        manager = fake_manager()
        entered, release = threading.Event(), threading.Event()
        def infer(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test release timed out")
            return "text", "Chinese"
        manager._infer_sync = Mock(side_effect=infer)
        first = asyncio.create_task(manager._transcribe_local([0.0] * 160, None, ""))
        queued = None
        try:
            await wait_for_event(entered)
            queued = asyncio.create_task(manager._transcribe_local([0.0] * 160, None, ""))
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertEqual(manager.scheduler.snapshot()["queued"], 1)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.assertEqual(manager.scheduler.snapshot()["queued"], 0)
            self.assertEqual(manager.active_jobs, 1)
            release.set()
            await asyncio.wait_for(first, timeout=5)
            manager._infer_sync.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(*[t for t in (first, queued) if t is not None], return_exceptions=True)

    async def test_worker_error_releases_locks_and_job_counter(self):
        manager = fake_manager()
        manager._infer_sync = Mock(side_effect=ValueError("inference failed"))
        with self.assertRaisesRegex(ValueError, "inference failed"):
            await manager._transcribe_local([0.0] * 160, None, "")
        self.assertFalse(manager.infer_lock.locked())
        self.assertFalse(manager.scheduler.busy)
        self.assertEqual(manager.active_jobs, 0)

    async def test_worker_error_after_cancellation_does_not_replace_cancel(self):
        manager = fake_manager()
        entered, release = threading.Event(), threading.Event()
        def infer(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test release timed out")
            raise ValueError("inference failed during cancellation")
        manager._infer_sync = infer
        task = asyncio.create_task(manager._transcribe_local([0.0] * 160, None, ""))
        try:
            await wait_for_event(entered)
            await cancel_repeatedly(task)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            self.assertEqual(manager.active_jobs, 0)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_load_keeps_lock_through_repeated_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            manager = ModelManager(Path(tmp))
            manager.schedule_unload = Mock()
            def load():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test release timed out")
                manager.worker = SimpleNamespace(is_alive=lambda: True)
            manager._start_worker_sync = load
            task = asyncio.create_task(manager.load())
            try:
                await wait_for_event(entered)
                await cancel_repeatedly(task)
                self.assertTrue(manager.load_lock.locked())
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                self.assertEqual(manager.status, "ready")
                self.assertFalse(manager.load_lock.locked())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_load_after_cancellation_still_reports_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            manager = ModelManager(Path(tmp))
            manager.schedule_unload = Mock()
            def load():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test release timed out")
                raise ValueError("load failed after owner cancelled")
            manager._start_worker_sync = load
            task = asyncio.create_task(manager.load())
            try:
                await wait_for_event(entered)
                await cancel_repeatedly(task)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                self.assertEqual(manager.status, "idle")
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_close_keeps_both_locks_until_stop_worker_finishes(self):
        manager = fake_manager()
        entered, release = threading.Event(), threading.Event()
        def stop():
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test release timed out")
            manager.worker = None
        manager._stop_worker_sync = stop
        task = asyncio.create_task(manager.close())
        try:
            await wait_for_event(entered)
            await cancel_repeatedly(task)
            self.assertTrue(manager.infer_lock.locked())
            self.assertTrue(manager.load_lock.locked())
            self.assertTrue(manager._unloading)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            self.assertFalse(manager.infer_lock.locked())
            self.assertFalse(manager.load_lock.locked())
            self.assertFalse(manager._unloading)
            self.assertEqual(manager.status, "idle")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


class RemoteCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def _remote_case(self, fail):
        entered, release, exited = (threading.Event() for _ in range(3))
        received = {}
        def run(kind, source, output, options, *, stop, **kwargs):
            received["stop"] = stop
            entered.set()
            try:
                if not release.wait(10):
                    raise RuntimeError("test release timed out")
                if fail:
                    raise ValueError("transport stopped")
                output.mkdir()
                (output / "transcript.json").write_text(json.dumps({
                    "text": "done", "language": "Chinese",
                    "segments": [{"start": 0, "end": .01, "text": "done"}],
                }), encoding="utf-8")
                return {"provider": "remote"}
            finally:
                exited.set()

        with tempfile.TemporaryDirectory() as tmp, patch(
            "qwen_asr_app.inference_providers.remote_client", return_value=Mock(run=run)
        ):
            folder = Path(tmp)
            task = asyncio.create_task(RemoteASRProvider().transcribe_segments(
                [0.0] * 160, None, "", 384, {}, operation_dir=folder
            ))
            try:
                await wait_for_event(entered)
                await cancel_repeatedly(task, 8)
                self.assertTrue(received["stop"].is_set())
                self.assertFalse(task.done())
                self.assertFalse(exited.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                self.assertTrue(exited.is_set())
                self.assertTrue((folder / "request.json").is_file())
                self.assertEqual((folder / "result/transcript.json").exists(), not fail)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_remote_error_after_cancel_is_drained(self):
        await self._remote_case(fail=True)

    async def test_remote_completion_after_cancel_keeps_durable_result(self):
        await self._remote_case(fail=False)

    async def test_missing_operation_directory_still_fails_without_transport(self):
        with patch("qwen_asr_app.inference_providers.remote_client") as client:
            with self.assertRaises(ValueError):
                await RemoteASRProvider().transcribe_segments([0.0] * 160, None, "", 384, {})
            client.assert_not_called()


class ExistingWorkerProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def _worker_case(self, conversion):
        entered, release, exited = (threading.Event() for _ in range(3))
        stop = threading.Event()
        def work(*, stop=None):
            entered.set()
            try:
                if not release.wait(10):
                    raise RuntimeError("test release timed out")
                return "written"
            finally:
                exited.set()

        task = asyncio.create_task(
            conversion_worker(work, stop=stop) if conversion else file_worker(work)
        )
        try:
            await wait_for_event(entered)
            await cancel_repeatedly(task, 8)
            self.assertFalse(task.done())
            self.assertFalse(exited.is_set())
            if conversion:
                self.assertTrue(stop.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            self.assertTrue(exited.is_set())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_pdf_conversion_existing_protection_survives_repeated_cancel(self):
        await self._worker_case(conversion=True)

    async def test_file_publication_existing_protection_survives_repeated_cancel(self):
        await self._worker_case(conversion=False)


if __name__ == "__main__":
    unittest.main()
