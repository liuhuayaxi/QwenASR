from tests import isolation
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from qwen_asr_app.model_runtime import ModelManager

class CancelledLoadReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_failed_load_does_not_mark_retained_child_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ModelManager(Path(tmp))
            manager.schedule_unload = Mock()
            entered, release = threading.Event(), threading.Event()
            retained = SimpleNamespace(is_alive=lambda: True)
            def fail():
                manager.worker = retained
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test release timeout')
                raise RuntimeError('model load failed; child could not stop')
            manager._start_worker_sync = fail
            task = asyncio.create_task(manager.load())
            try:
                async def wait_entered():
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                await asyncio.wait_for(wait_entered(), 2)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertEqual(manager.status, 'error')
                self.assertIs(manager.worker, retained)
                self.assertFalse(manager.load_lock.locked())
                with self.assertRaisesRegex(RuntimeError, "尚未加载成功"):
                    await manager.load()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
