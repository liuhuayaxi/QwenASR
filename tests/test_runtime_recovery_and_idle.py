"""Runtime regressions with real receipts/WAV/processes and synthetic inference."""
from tests import isolation  # noqa: F401 -- private temporary application paths

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

import httpx
import numpy as np

from qwen_asr_app.inference_providers import DEFAULTS
from qwen_asr_app.live_session import TranscriptionSession
from qwen_asr_app.model_runtime import ModelManager
from qwen_asr_app.remote_backlog import retry_once
from qwen_asr_app.remote_classroom import ClassroomNotAccepted, transcribe_clip
from qwen_asr_app.remote_operations import ResultNeedsRepair, mark_local_fallback, operation_receipt_lock
from qwen_asr_app.storage import RecordingRecord


def synthetic_model_worker(model_path, requests, results):
    """Only the heavyweight MLX implementation is replaced; IPC is real."""
    results.put({'type': 'ready'})
    while True:
        value = requests.get()
        if value['type'] == 'stop':
            return
        results.put({'type': 'result', 'id': value['id'], 'text': 'synthetic transcript', 'language': 'Chinese'})


async def until(check, seconds=5):
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(.01)


def recording(directory):
    value = RecordingRecord(Path(directory), {'id': '20261019-runtime-test', 'title': 'synthetic',
        'category': '未分类', 'kind': 'microphone', 'status': 'active', 'sample_count': 0,
        'sample_rate': 16000, 'next_segment_id': 1, 'audio_filename': 'recording.aac',
        'working_audio_filename': '.recording-recovery.wav'})
    value._create_empty_wav()
    value.save_all()
    return value


class ConnectedIdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_idle_worker_exits_and_next_transcription_reloads(self):
        with tempfile.TemporaryDirectory() as raw:
            manager = ModelManager(Path(raw))
            manager.client_connected()
            self.assertEqual(manager.idle_release_seconds, 30)
            with patch('qwen_asr_app.model_runtime.model_worker_main', synthetic_model_worker), \
                 patch.object(ModelManager, 'idle_release_seconds', new_callable=PropertyMock, return_value=.05):
                try:
                    await manager.load(force_local=True, inference_config={'asr_provider': 'local'})
                    first = manager.worker
                    await until(lambda: manager.worker is None)
                    self.assertFalse(first.is_alive())
                    self.assertEqual(manager.status, 'idle')
                    self.assertEqual(manager.connected_clients, 1)
                    value = await manager.transcribe(np.zeros(1600), None, '', inference_config={'asr_provider': 'local'})
                    self.assertEqual(value[0], 'synthetic transcript')
                    second = manager.worker
                    self.assertNotEqual(first.pid, second.pid)
                    await until(lambda: manager.worker is None)
                    self.assertFalse(second.is_alive())
                finally:
                    await manager.close()

    async def test_queued_realtime_job_pins_worker_until_transcription_finishes(self):
        with tempfile.TemporaryDirectory() as raw:
            manager = ModelManager(Path(raw))
            manager.client_connected()
            with patch('qwen_asr_app.model_runtime.model_worker_main', synthetic_model_worker), \
                 patch.object(ModelManager, 'idle_release_seconds', new_callable=PropertyMock, return_value=.05):
                try:
                    await manager.load(force_local=True, inference_config={'asr_provider': 'local'})
                    worker = manager.worker
                    async with manager.scheduler.slot(0):
                        waiting = asyncio.create_task(manager.transcribe(np.zeros(1600), None, '', priority=0,
                            inference_config={'asr_provider': 'local'}))
                        await until(lambda: manager.active_jobs == 1)
                        await asyncio.sleep(.1)
                        self.assertIs(manager.worker, worker)
                        self.assertTrue(worker.is_alive())
                    self.assertEqual((await waiting)[0], 'synthetic transcript')
                    await until(lambda: manager.worker is None)
                finally:
                    await manager.close()

    async def test_repeated_warmup_restores_idle_timer(self):
        manager = ModelManager(Path('/unused'))
        manager.worker = SimpleNamespace(is_alive=lambda: True)
        manager.client_connected()
        manager._unload_worker = AsyncMock(side_effect=lambda: setattr(manager, 'worker', None))
        with patch.object(ModelManager, 'idle_release_seconds', new_callable=PropertyMock, return_value=.01):
            await manager.load(force_local=True, inference_config={'asr_provider': 'local'})
            await until(lambda: manager.worker is None)
        manager._unload_worker.assert_awaited_once()


class ClassroomFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_failure_falls_back_once_without_remote_compute(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)/'remote-operations/live/clip'
            manager = ModelManager(Path(raw))
            manager._transcribe_local = AsyncMock(return_value=('local result', 'Chinese', .1))
            with patch('qwen_asr_app.remote_classroom.capabilities', side_effect=httpx.ConnectError('offline')), \
                 patch('qwen_asr_app.remote_classroom.request') as compute:
                metrics = {}
                placements = []
                result = await manager.transcribe(np.zeros(1600), None, '', operation_dir=folder,
                    inference_config={**DEFAULTS, 'asr_provider': 'remote_preferred'}, metrics=metrics,
                    on_provider=placements.append)
            self.assertEqual(result[0], 'local result')
            manager._transcribe_local.assert_awaited_once()
            compute.assert_not_called()
            self.assertTrue(metrics['fallback'])
            self.assertEqual([value['provider'] for value in placements], ['remote', 'local'])
            self.assertTrue(placements[-1]['fallback'])
            self.assertEqual(json.loads((folder/'classroom-receipt.json').read_text())['state'], 'local_fallback')
            # A persisted local decision cannot be switched back to remote by retry.
            with patch('qwen_asr_app.remote_classroom.capabilities') as caps:
                with self.assertRaises(ClassroomNotAccepted):
                    transcribe_clip(folder, {}, folder/'audio.wav', None, '', 384)
            caps.assert_not_called()

    async def test_legacy_or_submitting_receipt_never_allows_offline_fallback(self):
        for state in ('pending', 'submitting'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as raw:
                folder = Path(raw)/'remote-operations/live/clip'
                from qwen_asr_app.inference_providers import prepare_audio
                cfg = {**DEFAULTS, 'asr_provider': 'remote_preferred'}
                prepare_audio(folder, np.zeros(1600), None, '', 384, cfg)
                (folder/'classroom-receipt.json').write_text(json.dumps({'state': state,
                    'operation_id': 'live-original-operation', 'audio_sha256': hashlib.sha256((folder/'audio.wav').read_bytes()).hexdigest()}))
                manager = ModelManager(Path(raw))
                manager._transcribe_local = AsyncMock()
                with patch('qwen_asr_app.remote_classroom.capabilities', side_effect=httpx.ConnectError('offline')):
                    with self.assertRaises(httpx.ConnectError):
                        await manager.transcribe(np.zeros(1600), None, '', operation_dir=folder, inference_config=cfg)
                manager._transcribe_local.assert_not_awaited()
                self.assertEqual(json.loads((folder/'classroom-receipt.json').read_text())['state'], state)

    async def test_remote_only_preflight_failure_keeps_prepared_audio_for_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)/'remote-operations/live/clip'
            manager = ModelManager(Path(raw))
            manager._transcribe_local = AsyncMock()
            with patch('qwen_asr_app.remote_classroom.capabilities', side_effect=httpx.ConnectError('offline')):
                with self.assertRaises(ClassroomNotAccepted):
                    await manager.transcribe(np.zeros(1600), None, '', operation_dir=folder,
                        inference_config={**DEFAULTS, 'asr_provider': 'remote_only'})
            manager._transcribe_local.assert_not_awaited()
            self.assertTrue((folder/'audio.wav').is_file())
            self.assertEqual(json.loads((folder/'classroom-receipt.json').read_text())['state'], 'proven_not_accepted')

    async def test_fallback_cannot_replace_a_concurrently_submitting_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            receipt = Path(raw)/'classroom-receipt.json'
            receipt.write_text(json.dumps({'state': 'proven_not_accepted'}))
            with operation_receipt_lock(receipt):
                with self.assertRaises(ResultNeedsRepair):
                    mark_local_fallback(Path(raw)/'receipt.json')
            receipt.write_text(json.dumps({'state': 'submitting'}))
            with self.assertRaises(ValueError):
                mark_local_fallback(Path(raw)/'receipt.json')
            self.assertEqual(json.loads(receipt.read_text())['state'], 'submitting')


class CrashCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_tail_inherits_remote_power_policy_and_gets_durable_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            record = recording(raw)
            routing = {**DEFAULTS, 'asr_provider': 'remote_only', 'classroom_power_saving': True}
            record.metadata['inference_settings'] = routing
            record.append_audio(np.zeros(3 * 16000, dtype=np.float32))
            record.save_metadata()
            record = RecordingRecord.load(record.directory)
            manager = SimpleNamespace(transcribe=AsyncMock(side_effect=RuntimeError('offline')), load=AsyncMock())
            session = TranscriptionSession(SimpleNamespace(send_json=AsyncMock()), manager, SimpleNamespace())
            session.recording = record
            with patch('qwen_asr_app.live_session.PRIVATE_ROOT', Path(raw)/'private'):
                await session.recover_pending()
                await session.drain()
            supplied = manager.transcribe.await_args.kwargs
            self.assertEqual(supplied['inference_config']['asr_provider'], 'remote_only')
            self.assertTrue(supplied['inference_config']['classroom_power_saving'])
            snapshot = record.metadata['pending_segments']['1']['config_snapshot']
            self.assertEqual(snapshot['_remote_operation_dir'], supplied['operation_dir'])
            self.assertTrue((Path(supplied['operation_dir'])/'request.json').is_file())
            self.assertTrue((Path(supplied['operation_dir'])/'audio.wav').is_file())
            manager.load.assert_not_awaited()

    async def test_resume_uses_saved_capture_route_before_todays_setting(self):
        with tempfile.TemporaryDirectory() as raw:
            record = recording(raw)
            capture = {'inference_settings': {**DEFAULTS, 'asr_provider': 'remote_only'}, 'context': 'old lesson'}
            record.metadata.update(capture_config_snapshot=capture, inference_settings={'asr_provider': 'local'})
            record.append_audio(np.zeros(16000, dtype=np.float32))
            manager = SimpleNamespace(transcribe=AsyncMock(side_effect=RuntimeError('offline')), load=AsyncMock())
            store = SimpleNamespace(start_or_resume=lambda *args: (record, False))
            session = TranscriptionSession(SimpleNamespace(send_json=AsyncMock()), manager, store)
            with patch('qwen_asr_app.live_session.PRIVATE_ROOT', Path(raw)/'private'), \
                 patch('qwen_asr_app.inference_providers.inference_settings.for_task', return_value={'asr_provider': 'local'}):
                await session.start_or_resume()
                await session.drain()
            self.assertEqual(manager.transcribe.await_args.kwargs['inference_config']['asr_provider'], 'remote_only')
            self.assertEqual(record.metadata['capture_config_snapshot']['inference_settings']['asr_provider'], 'local')
            self.assertEqual(record.metadata['pending_segments']['1']['config_snapshot']['context'], 'old lesson')

    async def test_preferred_backlog_is_replayed_from_same_durable_audio(self):
        with tempfile.TemporaryDirectory() as raw:
            record = recording(raw)
            snapshot = {'inference_settings': {**DEFAULTS, 'asr_provider': 'remote_preferred'}}
            with patch('qwen_asr_app.live_session.PRIVATE_ROOT', Path(raw)/'private'):
                TranscriptionSession.prepare_remote_segment(record, 1, np.zeros(16000), snapshot)
            record.metadata['pending_segments'] = {'1': {'id': 1, 'start': 0, 'end': 1, 'config_snapshot': snapshot}}
            store = SimpleNamespace(remote_backlog_ids=lambda: [record.id], find=lambda ident: record)
            manager = SimpleNamespace(transcribe=AsyncMock(return_value=('', 'Chinese', .1)))
            self.assertTrue(await retry_once(store, manager, {}, lambda ident: False))
            self.assertEqual(manager.transcribe.await_args.kwargs['operation_dir'], snapshot['_remote_operation_dir'])
            self.assertEqual(record.metadata['pending_segments'], {})

    async def test_remote_capture_does_not_allocate_unused_prefix_preview(self):
        with tempfile.TemporaryDirectory() as raw:
            record = recording(raw)
            session = TranscriptionSession(SimpleNamespace(send_json=AsyncMock()), SimpleNamespace(), SimpleNamespace())
            session.recording, session.accepting_audio, session.speaking = record, True, True
            session.config['inference_settings'] = {'asr_provider': 'remote_only'}
            session.chunks = [np.ones(6 * 16000, dtype=np.float32)*.1]
            session.sample_count = 6 * 16000
            with patch('qwen_asr_app.live_session.np.concatenate') as concatenate:
                await session.ingest((np.ones(1600, dtype='<f4')*.1).tobytes())
            concatenate.assert_not_called()
            self.assertFalse(session.tasks)


if __name__ == '__main__':
    unittest.main()
