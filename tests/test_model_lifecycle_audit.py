"""Test model process ownership without loading MLX or model weights."""
from tests import isolation  # noqa: F401
import asyncio
import multiprocessing
import os
import signal
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from qwen_asr_app.model_runtime import ModelManager


def ignore_termination(ready):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ready.set()
    while True:
        time.sleep(1)


def process_double(*, stop_works=False, terminate_works=False, kill_works=True):
    state = {"alive": True}
    worker = Mock()
    worker.is_alive.side_effect = lambda: state["alive"]
    def terminate():
        if terminate_works:
            state["alive"] = False
    def kill():
        if kill_works:
            state["alive"] = False
    worker.terminate.side_effect = terminate
    worker.kill.side_effect = kill
    manager = ModelManager(Path("/not-loaded"))
    manager.worker = worker
    manager.request_queue = Mock()
    manager.result_queue = Mock()
    if stop_works:
        manager.request_queue.put.side_effect = lambda *args, **kwargs: state.update(alive=False)
    return manager, worker, state


class ModelProcessTests(unittest.TestCase):
    def test_graceful_exit_does_not_send_signals(self):
        manager, worker, state = process_double(stop_works=True)
        manager._stop_worker_sync()
        self.assertFalse(state["alive"])
        worker.terminate.assert_not_called()
        worker.kill.assert_not_called()
        self.assertIsNone(manager.worker)

    def test_terminate_is_enough_for_cooperative_process(self):
        manager, worker, state = process_double(terminate_works=True)
        manager._stop_worker_sync()
        self.assertFalse(state["alive"])
        worker.terminate.assert_called_once()
        worker.kill.assert_not_called()
        self.assertIsNone(manager.worker)

    def test_kill_is_used_when_terminate_does_not_stop_process(self):
        manager, worker, state = process_double()
        request, result = manager.request_queue, manager.result_queue
        manager._stop_worker_sync()
        self.assertFalse(state["alive"])
        worker.terminate.assert_called_once()
        worker.kill.assert_called_once()
        request.close.assert_called_once()
        result.close.assert_called_once()
        self.assertIsNone(manager.worker)
        self.assertIsNone(manager.request_queue)
        self.assertIsNone(manager.result_queue)

    def test_unreaped_process_retains_its_queues_and_reference(self):
        manager, worker, _ = process_double(kill_works=False)
        request, result = manager.request_queue, manager.result_queue
        with self.assertRaises(RuntimeError):
            manager._stop_worker_sync()
        self.assertIs(manager.worker, worker)
        self.assertIs(manager.request_queue, request)
        self.assertIs(manager.result_queue, result)
        request.close.assert_not_called()
        result.close.assert_not_called()

    def test_new_process_is_not_spawned_if_previous_process_did_not_exit(self):
        manager, worker, _ = process_double(kill_works=False)
        with patch("qwen_asr_app.model_runtime.mp.get_context") as context:
            with self.assertRaises(RuntimeError):
                manager._start_worker_sync()
            context.assert_not_called()
        self.assertIs(manager.worker, worker)

    @unittest.skipUnless(os.name == "posix", "SIGTERM/SIGKILL lifecycle is POSIX-specific")
    def test_real_process_ignoring_sigterm_is_reaped(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        process = context.Process(target=ignore_termination, args=(ready,), daemon=True)
        manager = ModelManager(Path("/not-loaded"))
        try:
            process.start()
            self.assertTrue(ready.wait(5), "child did not reach its signal handler")
            manager.worker = process
            manager._stop_worker_sync()
            self.assertFalse(process.is_alive(), "live child abandoned by model manager")
            self.assertEqual(process.exitcode, -signal.SIGKILL)
            self.assertIsNone(manager.worker)
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            process.close()


class UnloadStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_unload_does_not_claim_gpu_memory_was_released(self):
        manager, worker, _ = process_double(kill_works=False)
        with self.assertRaises(RuntimeError):
            await manager.close()
        self.assertEqual(manager.status, "error")
        self.assertIs(manager.worker, worker)
        self.assertFalse(manager._unloading)
        self.assertFalse(manager.load_lock.locked())
        self.assertFalse(manager.infer_lock.locked())

    async def test_empty_manager_can_be_closed(self):
        manager = ModelManager(Path("/not-loaded"))
        await manager.close()
        self.assertEqual(manager.status, "idle")
        self.assertIsNone(manager.worker)


if __name__ == "__main__":
    unittest.main()
