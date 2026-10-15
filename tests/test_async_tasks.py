"""Owned tasks must finish before their cancelled owner releases resources."""
from tests import isolation  # noqa: F401
import asyncio
import threading
import unittest

from qwen_asr_app.async_tasks import await_owned_task


class OwnedTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_already_finished_result(self):
        task = asyncio.create_task(asyncio.sleep(0, result=42))
        self.assertEqual(await await_owned_task(task), 42)
        self.assertEqual(await await_owned_task(task), 42)

    async def test_worker_error_is_not_hidden(self):
        async def fail():
            raise ValueError("worker failed")
        task = asyncio.create_task(fail())
        with self.assertRaisesRegex(ValueError, "worker failed"):
            await await_owned_task(task)

    async def test_independently_cancelled_worker_does_not_loop_forever(self):
        task = asyncio.create_task(asyncio.sleep(60))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(await_owned_task(task), timeout=1)

    async def _cancel_case(self, count, fail=False):
        started, release = asyncio.Event(), asyncio.Event()
        stop = threading.Event()

        async def operation():
            started.set()
            await release.wait()
            if fail:
                raise ValueError("worker failed after cancellation")
            return "completed"

        inner = asyncio.create_task(operation())
        owner = asyncio.create_task(await_owned_task(inner, stop=stop))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            for _ in range(count):
                owner.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
                self.assertFalse(owner.done())
                self.assertFalse(inner.done())
                self.assertFalse(inner.cancelled())
                self.assertTrue(stop.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(owner, timeout=1)
            self.assertTrue(inner.done())
            self.assertFalse(inner.cancelled())
            if fail:
                self.assertIsInstance(inner.exception(), ValueError)
            else:
                self.assertEqual(inner.result(), "completed")
        finally:
            release.set()
            await asyncio.gather(owner, inner, return_exceptions=True)

    async def test_single_cancellation_waits_for_owned_work(self):
        await self._cancel_case(1)

    async def test_repeated_cancellation_waits_for_owned_work(self):
        await self._cancel_case(32)

    async def test_worker_failure_after_cancel_preserves_cancellation(self):
        await self._cancel_case(2, fail=True)

    async def test_no_stop_signal_without_cancellation(self):
        stop = threading.Event()
        task = asyncio.create_task(asyncio.sleep(0, result="ok"))
        self.assertEqual(await await_owned_task(task, stop=stop), "ok")
        self.assertFalse(stop.is_set())


if __name__ == "__main__":
    unittest.main()
