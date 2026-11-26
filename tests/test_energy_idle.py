from tests import isolation  # noqa: F401

import asyncio
import signal
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from qwen_asr_app.background_wakeup import ChangeSignal, library_changes
from qwen_asr_app.knowledge.coordinator import KnowledgeCoordinator
from qwen_asr_app.remote_backlog import recovery_loop
from qwen_asr_app.server import IdleServer
import uvicorn


class EnergyIdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_library_watch_delivers_change_and_stops_cleanly(self):
        from qwen_asr_app.shared_library import LibraryWatch
        from qwen_asr_app.library_layout import SHARED_LAYOUT
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve(); (root/'A').mkdir()
            settings=SimpleNamespace(storage_root=root,library_layout=SHARED_LAYOUT)
            recordings=SimpleNamespace(refresh=Mock())
            watcher=LibraryWatch(settings,recordings)
            changed=asyncio.Event()
            def on_change(paths):
                if paths and any(str(p).endswith('notes.md') for p in paths):
                    changed.set()
            library_changes.callbacks.add(on_change)
            try:
                with patch('qwen_asr_app.shared_library.discover_courses'):
                    await watcher.start()
                    await asyncio.sleep(.2)
                    (root/'A'/'notes.md').write_text('synthetic change')
                    await asyncio.wait_for(changed.wait(),3)
                    self.assertGreaterEqual(recordings.refresh.call_count,2)
                    await asyncio.wait_for(watcher.close(),2)
            finally:
                library_changes.callbacks.discard(on_change)
                if watcher.task and not watcher.task.done():
                    await watcher.close()

    async def test_broadcast_preserves_change_during_work_and_wakes_all_consumers(self):
        changes = ChangeSignal()
        version = changes.version
        changes.notify()
        await asyncio.wait_for(changes.wait(version), .1)
        version = changes.version
        tasks = [asyncio.create_task(changes.wait(version)) for _ in range(3)]
        await asyncio.sleep(0)
        changes.notify()
        await asyncio.wait_for(asyncio.gather(*tasks), .1)
        version = changes.version
        await changes.wait(version, .001)
        task = asyncio.create_task(changes.wait(version))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        changes.notify()
        await task

    async def test_worker_thread_notifications_are_delivered_on_service_loop(self):
        changes = ChangeSignal()
        callback = Mock()
        changes.callbacks.add(callback)
        waiter = asyncio.create_task(changes.wait(changes.version))
        await asyncio.sleep(0)
        await asyncio.to_thread(changes.notify, ['synthetic/session.json'])
        await asyncio.wait_for(waiter, .2)
        callback.assert_called_once_with(['synthetic/session.json'])

    async def test_retry_waits_for_saved_deadline_and_then_returns_to_idle(self):
        from qwen_asr_app.classroom_intelligence.service import ClassroomIntelligenceService
        rows = [{'status':'failed', 'job':{'retryable':True, 'next_retry_at':time.time()+.04}}]
        retry = AsyncMock()
        def complete():
            if retry.await_count > 1:
                rows.clear()
        retry.side_effect=complete
        service = SimpleNamespace(_retry_once=retry,
            _note_job_index=SimpleNamespace(records=lambda _: [('synthetic',rows)]), store=None)
        task=asyncio.create_task(ClassroomIntelligenceService._retry_loop(service))
        try:
            await asyncio.sleep(.25)
            self.assertEqual(retry.await_count, 2)
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)

    def test_both_server_commands_require_original_process_identity_and_token(self):
        from qwen_asr_app import service_control as control
        import sys
        for module in ('uvicorn','qwen_asr_app.server'):
            row={'state':'S','started':'synthetic',
                 'command':f'{sys.executable} -m {module} app:app --host 127.0.0.1 --port {control.PORT} --timeout-graceful-shutdown 10'}
            with patch.object(control.subprocess,'check_output',return_value='QWEN_ASR_INSTANCE=synthetic-token'):
                self.assertTrue(control.owned_process({'pid':42,'token':'synthetic-token','started':'synthetic'},{42:row}))
                with self.assertRaisesRegex(RuntimeError,'启动标识不匹配'):
                    control.owned_process({'pid':42,'token':'wrong','started':'synthetic'},{42:row})

    async def test_knowledge_checks_only_dirty_course_and_recovers_pending_work(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for category in ['A', 'B']:
                (root/category).mkdir()
                (root/category/'notes.md').write_text('original')
            active = {'A': False, 'B': False}
            service = SimpleNamespace(settings=SimpleNamespace(storage_root=root, categories=['A','B']),
                store=SimpleNamespace(get_course=lambda _: True,
                                      get_active_job_for_category=lambda c: active[c]), schedule=Mock())
            coordinator = KnowledgeCoordinator(service, interval=.01, reconcile_interval=60)
            with patch.object(coordinator, 'fingerprint', wraps=coordinator.fingerprint) as fp:
                await coordinator.start()
                await asyncio.sleep(.06)
                self.assertEqual(fp.call_count, 2)
                service.schedule.reset_mock(); fp.reset_mock()
                await asyncio.sleep(.04)
                fp.assert_not_called()
                active['A'] = True
                (root/'A'/'notes.md').write_text('changed')
                library_changes.notify([str(root/'A'/'notes.md')])
                await asyncio.sleep(.06)
                self.assertEqual({c.args[0] for c in fp.call_args_list}, {'A'})
                service.schedule.assert_not_called()
                active['A'] = False
                await asyncio.sleep(.06)
                service.schedule.assert_called_once_with('A')
                await coordinator.close()
                self.assertNotIn(coordinator.mark_dirty, library_changes.callbacks)

    async def test_wake_rechecks_saved_fingerprints_without_rescheduling_unchanged_courses(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'A').mkdir();source=root/'A/notes.md';source.write_text('first')
            service=SimpleNamespace(settings=SimpleNamespace(storage_root=root,categories=['A']),
                store=SimpleNamespace(get_course=lambda _:True,get_active_job_for_category=lambda _:False),
                schedule=Mock())
            coordinator=KnowledgeCoordinator(service,interval=.01,reconcile_interval=60)
            coordinator.stamps['A']=coordinator.fingerprint('A')
            await coordinator.start()
            try:
                await asyncio.sleep(.04)
                service.schedule.assert_not_called()
                source.write_text('changed after previous standby')
                library_changes.notify([str(source)])
                await asyncio.sleep(.05)
                service.schedule.assert_called_once_with('A')
            finally:
                await coordinator.close()

    async def test_empty_backlog_sleeps_until_change_without_periodic_gc(self):
        store = SimpleNamespace(remote_backlog_ids=Mock(return_value=[]))
        cleanup = Mock()
        with patch('qwen_asr_app.remote_backlog.retry_once', new_callable=AsyncMock, return_value=False) as retry:
            task = asyncio.create_task(recovery_loop(store, None, {}, lambda _:False, on_idle=cleanup))
            try:
                await asyncio.sleep(.03)
                self.assertEqual(retry.await_count, 1)
                cleanup.assert_not_called()
                library_changes.notify()
                await asyncio.sleep(.03)
                self.assertEqual(retry.await_count, 2)
                cleanup.assert_not_called()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_backup_disabled_waits_and_configuration_wakes_due_run(self):
        from qwen_asr_app.drive_backup import DriveBackup
        class BackupFixture(DriveBackup):
            def start(self):
                self.runs += 1
                self.next_run = time.monotonic()+60
        manager = BackupFixture.__new__(BackupFixture)
        manager.runs=0;manager.closing=False;manager.load_error=None;manager.task=None
        manager.pending_auth=None;manager.can_run=Mock(return_value=True)
        manager.state={'options':{'automatic':False}}
        manager.public=lambda:{'connected':True}
        manager.schedule_changed=asyncio.Event();manager.next_run=time.monotonic()-1
        task=asyncio.create_task(manager.automatic_loop())
        try:
            await asyncio.sleep(.03)
            self.assertEqual(manager.runs,0)
            self.assertEqual(manager.can_run.call_count,1)
            manager.state['options']['automatic']=True;manager.schedule_changed.set()
            await asyncio.sleep(.03)
            self.assertEqual(manager.runs,1)
            await asyncio.sleep(.03)
            self.assertEqual(manager.runs,1)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_server_sigterm_wakes_housekeeping_without_waiting_one_second(self):
        server=IdleServer(uvicorn.Config('synthetic:app'))
        server.on_tick=AsyncMock(side_effect=lambda _:server.should_exit)
        task=asyncio.create_task(server.main_loop())
        await asyncio.sleep(.01)
        server.handle_exit(signal.SIGTERM,None)
        await asyncio.wait_for(task,.1)
        self.assertTrue(server.should_exit)
        self.assertEqual([c.args[0] for c in server.on_tick.call_args_list],[0,10])
