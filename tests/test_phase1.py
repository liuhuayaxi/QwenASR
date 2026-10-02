from tests import isolation

import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import soundfile as sf
from fastapi import HTTPException

from qwen_asr_app.scheduler import PriorityGate
from qwen_asr_app.model_runtime import ModelManager
from qwen_asr_app.course_stream import CourseStreamJob, CourseStreamManager
from qwen_asr_app.storage import RecordingRecord
from qwen_asr_app.batch_pipeline import SegmentCorrectionPipeline
from tests.test_course_stream import make_record, until


def model_stub():
    return SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                           transcribe=AsyncMock(return_value=('中文 English 不是 12.5 mA', 'Chinese', .01)))


def manager_for(root, model):
    class Store:
        paths = {}
        def list_records(self):
            return [RecordingRecord.load(p.parent) for p in Path(root).rglob('session.json') if '.versions' not in p.parts]
        def find(self, ident):
            return next((r for r in self.list_records() if r.id == ident), None)
    return CourseStreamManager(model, Store(), SimpleNamespace(storage_root=Path(root), add_category=lambda x:x), set())


def source(ident):
    return {'video_id': ident, 'segment_id': 1, 'duration': 3}


def request(key, sources):
    return {'key': key, 'duration': 3 * len(sources), 'sources': sources,
            'config': {'title': '测试课 ' + key, 'ai_correction': False}}


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_overtakes_background_with_bounded_fairness(self):
        gate = PriorityGate(max_overtakes=2)
        order = []
        async def run(label, priority):
            async with gate.slot(priority):
                order.append(label)
                await asyncio.sleep(0)
        async with gate.slot():
            tasks = [asyncio.create_task(run('background', 10))]
            tasks += [asyncio.create_task(run('live' + str(i), 0)) for i in range(4)]
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        self.assertEqual(order, ['live0', 'live1', 'background', 'live2', 'live3'])

    async def test_cancel_waiter_does_not_release_running_slot(self):
        gate = PriorityGate()
        async with gate.slot():
            async def wait():
                async with gate.slot(0):
                    self.fail('cancelled request was run')
            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(gate.busy)
            self.assertEqual(gate.snapshot()['queued'], 0)
        async with gate.slot():
            self.assertTrue(gate.busy)

    async def test_cancel_running_model_drains_thread_before_next_inference(self):
        manager = ModelManager(Path('.'))
        manager.worker = SimpleNamespace(is_alive=lambda: True)
        manager.status = 'ready'
        manager.load = AsyncMock()
        manager.schedule_unload = lambda: None
        entered, release = threading.Event(), threading.Event()
        calls = []
        def infer(*args):
            calls.append(len(calls))
            entered.set()
            release.wait(2)
            return '课堂', 'Chinese'
        manager._infer_sync = infer
        audio = np.zeros(16000, np.float32)
        first = asyncio.create_task(manager.transcribe(audio, None, ''))
        await until(entered.is_set)
        first.cancel()
        metrics = {}
        second = asyncio.create_task(manager.transcribe(audio, None, '', priority=0, metrics=metrics))
        await asyncio.sleep(.03)
        self.assertEqual(len(calls), 1)
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await second
        self.assertEqual(len(calls), 2)
        self.assertGreater(metrics['queue_seconds'], .02)
        self.assertEqual(manager.performance()['realtime']['count'], 1)
        self.assertEqual(manager.active_jobs, 0)

    async def test_obsolete_preview_does_not_load_model(self):
        manager = ModelManager(Path('.'))
        manager.load = AsyncMock()
        result = await manager.transcribe(np.zeros(16000), None, '', priority=2, is_current=lambda: False)
        self.assertEqual(result[0], '')
        manager.load.assert_not_awaited()


class DurableCourseTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_concurrent_submissions_have_one_task_and_restart_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            model = model_stub()
            manager = manager_for(root, model)
            states = await asyncio.gather(*(manager.create(request('A', [source('A')])) for _ in range(10)))
            self.assertEqual(len({s['id'] for s in states}), 1)
            self.assertEqual(len(manager.store.list_records()), 1)
            await manager.close()
            restored = manager_for(root, model)
            self.assertEqual(restored.get(states[0]['id']).snapshot()['status'], 'interrupted')
            self.assertIsNone(restored.get(states[0]['id']).task)

    async def test_crash_after_audio_commit_before_metadata_recovers_one_chunk(self):
        with tempfile.TemporaryDirectory() as root:
            record = make_record(Path(root))
            model = model_stub()
            job = CourseStreamJob(record, {}, model, set())
            job.start()
            with patch.object(record, 'save_metadata', side_effect=OSError('simulated disk checkpoint crash')):
                with self.assertRaises(OSError):
                    await job.accept(0, bytes(3*16000*2), {'source': 1, 'samples': 0})
            loaded = RecordingRecord.load(record.directory)
            self.assertEqual(loaded.metadata['course_stream']['chunks'], [])
            # Simulate process death: discard memory without writing a shutdown checkpoint.
            with patch.object(record, 'save_all'), patch.object(record, 'save_metadata'):
                await job.stop()
            resumed = CourseStreamJob(loaded, {}, model, set())
            resumed.start()
            self.assertEqual(resumed.snapshot()['next_index'], 1)
            await resumed.accept(0, bytes(3*16000*2), {'source': 1, 'samples': 0})
            await resumed.finish_input(1)
            await resumed.task
            self.assertEqual(len(loaded.segments), 1)
            self.assertEqual(loaded.duration, 3)
            self.assertEqual(model.transcribe.await_count, 1)

    async def test_corrupt_committed_audio_is_rejected_before_model(self):
        with tempfile.TemporaryDirectory() as root:
            record = make_record(Path(root))
            model = model_stub()
            job = CourseStreamJob(record, {}, model, set())
            job.start()
            await job.accept(0, bytes(16000*2), {'source': 0, 'samples': 16000})
            await job.stop()
            (record.directory / '.course-chunks' / '00000.pcm').write_bytes(b'bad')
            loaded = RecordingRecord.load(record.directory)
            resumed = CourseStreamJob(loaded, {}, model, set())
            with self.assertRaises(ValueError):
                resumed.start()
            model.transcribe.assert_not_awaited()

    async def test_supplement_downloads_only_B_keeps_manual_edits_and_one_timeline(self):
        with tempfile.TemporaryDirectory() as root:
            model = model_stub()
            manager = manager_for(root, model)
            a = await manager.create(request('A', [source('A')]))
            first = manager.get(a['id'])
            pcm_a = (np.sin(np.arange(48000)*.05)*10000).astype('<i2')
            pcm_b = (np.cos(np.arange(48000)*.09)*10000).astype('<i2')
            await first.accept(0, pcm_a.tobytes(), {'source': 1, 'samples': 0})
            await first.finish_input(1)
            await first.task
            self.assertEqual(first.snapshot()['status'], 'completed')
            edited = manager.store.find(a['id'])
            edited.metadata['title'] = '我的课堂标题'
            edited.segments[0]['corrected'] = '人工确认：12.5 mA，English 保留'
            edited.save_all()
            # New process has no memory of the old import key.
            manager = manager_for(root, model)
            ab = await manager.create(request('AB', [source('A'), source('B')]))
            self.assertEqual(ab['id'], a['id'])
            self.assertEqual(ab['next_index'], 1)
            self.assertEqual(ab['resume'], {'source': 1, 'samples': 0})
            second = manager.get(ab['id'])
            await second.accept(1, pcm_b.tobytes(), {'source': 2, 'samples': 0})
            await second.finish_input(2)
            await second.task
            self.assertEqual(second.snapshot()['status'], 'completed')
            self.assertEqual(model.transcribe.await_count, 2)
            self.assertEqual(second.record.metadata['title'], '我的课堂标题')
            self.assertEqual(second.record.segments[0]['corrected'], '人工确认：12.5 mA，English 保留')
            self.assertEqual([s['start'] for s in second.record.segments], [0, 3])
            self.assertEqual(second.record.duration, 6)
            cached, _ = sf.read(second.record.directory / '.course-source.flac', dtype='int16')
            np.testing.assert_array_equal(cached, np.concatenate([pcm_a, pcm_b]))
            self.assertTrue(list((second.record.directory / '.versions').rglob('recording.aac')))
            # Ignore backup snapshots when counting top-level classroom records.
            self.assertEqual(len([p for p in Path(root).rglob('session.json') if '.versions' not in p.parts]), 1)
            replay = await manager.create(request('AB', [source('A'), source('B')]))
            self.assertEqual(replay['id'], a['id'])
            self.assertEqual(replay['status'], 'completed')

    async def test_slow_ai_allows_twelve_asr_segments_and_restart_keeps_finished_corrections(self):
        with tempfile.TemporaryDirectory() as root:
            model = model_stub()
            record = make_record(Path(root))
            release = asyncio.Event()
            async def correct(record, segment, config):
                await release.wait()
                segment.update(corrected='精修', ai_pending=False)
                record.upsert_segment(segment)
                return segment
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', correct):
                job = CourseStreamJob(record, {'ai_correction': True}, model, set())
                job.start()
                for i in range(12):
                    await job.accept(i, bytes(16000*2), {'source': 0, 'samples': (i+1)*16000})
                await until(lambda: len(record.segments) == 12)
                self.assertFalse(release.is_set())
                self.assertEqual(model.transcribe.await_count, 12)
                release.set()
                await until(lambda: all(s.get('corrected') for s in record.segments))
                await job.stop(paused=True)
                restored = CourseStreamJob(RecordingRecord.load(record.directory), {'ai_correction': True}, model, set())
                restored.start()
                await restored.finish_input(12)
                await restored.task
            self.assertEqual(model.transcribe.await_count, 12)
            self.assertTrue(all(s.get('corrected') for s in restored.record.segments))


class QueueRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_submitted_unpaused_items_run_and_settings_snapshot_is_used(self):
        from qwen_asr_app import smartclass_service as service
        state = {'paused': False, 'items': [
            {'group': {'id': 'A', 'title': 'A'}, 'state': 'interrupted', 'options': {'model': 'saved'}},
            {'group': {'id': 'B', 'title': 'B'}, 'state': 'paused', 'options': {}},
        ]}
        with patch.object(service, 'queue_state', state), patch.object(service, 'save_queue'), patch.object(service, 'save_imports'), patch.object(service, 'import_group', AsyncMock()) as run:
            service.stop_event.clear()
            await service.run_queue()
            run.assert_awaited_once_with({'id': 'A', 'title': 'A'}, {'model': 'saved'})
            self.assertEqual(state['items'][1]['state'], 'paused')
            await service.stop()
            self.assertTrue(state['paused'])
            await service.run_queue()
            self.assertEqual(run.await_count, 1)
        service.stop_event.clear()

    async def test_companion_shutdown_requires_local_headers_and_can_be_cancelled(self):
        from qwen_asr_app import smartclass_service as service
        import httpx
        headers = {'X-QwenASR-Control': 'control-test', 'X-QwenASR-Client': 'smartclass',
                   'Origin': 'http://127.0.0.1:8765'}
        with patch.dict('os.environ', {'QWEN_ASR_INSTANCE': 'control-test'}), patch.object(service, 'job', None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url='http://127.0.0.1:8765') as client:
                try:
                    ready = await client.post('/api/service/prepare-stop', headers=headers)
                    self.assertEqual(ready.status_code, 200)
                    stopped = await client.post('/api/import', headers=headers, json={'ids': ['A']})
                    self.assertEqual(stopped.status_code, 503)
                finally:
                    resumed = await client.post('/api/service/cancel-stop', headers=headers)
                    self.assertEqual(resumed.status_code, 200)
                    self.assertFalse(service.service_draining)

    async def test_shutdown_guard_refuses_active_model_and_blocks_new_mutations(self):
        import app
        import httpx
        transport = httpx.ASGITransport(app=app.app)
        with patch.dict('os.environ', {'QWEN_ASR_INSTANCE': 'test-instance'}), patch.object(app.model_manager, 'active_jobs', 1):
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8765') as client:
                denied = await client.post('/api/service/prepare-stop', headers={'X-QwenASR-Control': 'test-instance'})
                self.assertEqual(denied.status_code, 409)
        with patch.dict('os.environ', {'QWEN_ASR_INSTANCE': 'test-instance'}):
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8765') as client:
                try:
                    ready = await client.post('/api/service/prepare-stop', headers={'X-QwenASR-Control': 'test-instance'})
                    self.assertEqual(ready.status_code, 200)
                    rejected = await client.post('/api/course-streams', json={})
                    self.assertEqual(rejected.status_code, 503)
                finally:
                    await client.post('/api/service/cancel-stop', headers={'X-QwenASR-Control': 'test-instance'})


if __name__ == '__main__':
    unittest.main()
