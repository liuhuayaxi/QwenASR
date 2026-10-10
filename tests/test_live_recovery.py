from __future__ import annotations

from tests import isolation  # Must precede application imports.

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np

import app
from qwen_asr_app.ai import summary_instructions
from qwen_asr_app.batch_ai import build_note_summary, note_summary_chunks
from qwen_asr_app.context_budget import estimate_tokens
from qwen_asr_app.live_session import TranscriptionSession
from qwen_asr_app.storage import RecordingRecord, RecordingStore


def live_record(directory):
    record = RecordingRecord(directory, {
        'id': '20260909-120000-testlive', 'title': '测试课堂', 'category': '未分类',
        'kind': 'microphone', 'status': 'active', 'sample_count': 0, 'next_segment_id': 1,
        'sample_rate': 16000, 'audio_filename': 'recording.aac',
        'working_audio_filename': '.recording-recovery.wav',
    })
    record._create_empty_wav()
    record.save_all()
    return record


def store_for(record):
    store = RecordingStore.__new__(RecordingStore)
    store.active = record if record.metadata['status'] == 'active' else None
    store.paths = {record.id: record.directory}
    store._metadata_paths = lambda: [record.meta_path]
    return store


class LivePauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_resumed_local_recording_uses_saved_windows_for_new_audio(self):
        from qwen_asr_app.inference_providers import DEFAULTS
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            record.metadata['inference_settings'] = {'asr_provider': 'local'}
            manager = SimpleNamespace(load=AsyncMock(), transcribe=AsyncMock(return_value=('Windows 转写', 'Chinese', .1)))
            session = TranscriptionSession(SimpleNamespace(send_json=AsyncMock()), manager, store_for(record))
            session.recording, session.recovered = record, True
            selected = {**DEFAULTS, 'asr_provider': 'remote_only'}
            with patch('qwen_asr_app.inference_providers.inference_settings.for_task', return_value=selected), \
                 patch('qwen_asr_app.live_session.PRIVATE_ROOT', Path(directory)/'private'):
                await session.start_or_resume()
                manager.load.assert_not_awaited()
                samples = np.ones(16000, dtype=np.float32)*.2
                record.append_audio(samples)
                session.chunks, session.sample_count = [samples], len(samples)
                await session.detach_and_process('silence')
                await session.drain()
                self.assertEqual(manager.transcribe.await_args.kwargs['inference_config']['asr_provider'], 'remote_only')
                self.assertTrue(Path(manager.transcribe.await_args.kwargs['operation_dir'], 'request.json').is_file())
                manager.load.assert_not_awaited()

    async def test_remote_only_model_failure_does_not_load_local(self):
        from qwen_asr_app.inference_providers import DEFAULTS
        from qwen_asr_app.model_runtime import ModelManager
        manager = ModelManager(Path('/unused'))
        manager._transcribe_local = AsyncMock(side_effect=AssertionError('local inference'))
        with patch('qwen_asr_app.inference_providers.RemoteASRProvider.transcribe', new=AsyncMock(side_effect=RuntimeError('offline'))):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                await manager.transcribe(np.zeros(16000), None, '', inference_config={**DEFAULTS, 'asr_provider': 'remote_only'}, operation_dir='/unused')
        manager._transcribe_local.assert_not_awaited()

    async def test_pause_ack_does_not_wait_for_ai_and_resume_keeps_pending_work(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            ws = SimpleNamespace(send_json=AsyncMock())
            manager = SimpleNamespace(load=AsyncMock())
            session = TranscriptionSession(ws, manager, store_for(record))
            session.recording = record
            session.recovered = True
            session.accepting_audio = True
            released = asyncio.Event()
            session.track(released.wait())
            session.track_review(released.wait())
            await asyncio.wait_for(session.pause(), .3)
            self.assertFalse(session.accepting_audio)
            self.assertEqual(ws.send_json.await_args.args[0]['type'], 'paused')
            self.assertTrue(ws.send_json.await_args.args[0]['processing'])
            await session.ingest(np.ones(1600, dtype='<f4').tobytes())
            self.assertEqual(record.metadata['sample_count'], 0)
            await asyncio.wait_for(session.start_or_resume(), .3)
            self.assertTrue(session.accepting_audio)
            self.assertTrue(session.tasks)
            released.set()
            await session.pause(False)
            await session.drain()

    async def test_pause_during_speech_preserves_tail_while_asr_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            samples = np.ones(16000, dtype=np.float32) * .2
            record.append_audio(samples)
            released = asyncio.Event()
            async def transcribe(*args, **kwargs):
                await released.wait()
                return '最后一句', 'Chinese', .1
            session = TranscriptionSession(SimpleNamespace(send_json=AsyncMock()),
                                           SimpleNamespace(transcribe=transcribe), store_for(record))
            session.recording = record
            session.speaking = True
            session.chunks = [samples]
            session.sample_count = len(samples)
            await asyncio.wait_for(session.pause(), .3)
            self.assertTrue(record.metadata['pending_segments'])
            self.assertFalse(record.segments)
            released.set()
            await session.drain()
            self.assertEqual(record.segments[0]['raw'], '最后一句')
            self.assertEqual(record.metadata['pending_segments'], {})

    async def test_restart_recovers_missing_segment_and_unsubmitted_tail_once(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            record.append_audio(np.zeros(3 * 16000, dtype=np.float32))
            record.segments = [{'id': 2, 'start': 1, 'end': 2, 'raw': '已经保存的第二段'}]
            record.metadata.update(next_segment_id=3, pending_segments={'1': {'id': 1, 'start': 0, 'end': 1}})
            record.save_all()
            record = RecordingRecord.load(record.directory)
            calls = []
            async def transcribe(samples, *args, **kwargs):
                calls.append(len(samples))
                return '恢复片段', 'Chinese', .1
            session = TranscriptionSession(SimpleNamespace(send_json=AsyncMock()),
                                           SimpleNamespace(transcribe=transcribe), store_for(record))
            session.recording = record
            await session.recover_pending()
            await session.drain()
            await session.recover_pending()
            await session.drain()
            self.assertEqual(calls, [16000, 16000])
            self.assertEqual([s['id'] for s in record.segments], [1, 2, 3])
            self.assertEqual(record.segments[1]['raw'], '已经保存的第二段')
            self.assertEqual(record.metadata['pending_segments'], {})


class DurableJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_queued_job_releases_it_for_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            record.metadata['status'] = 'completed'
            record.save_metadata()
            store = store_for(record)
            with patch.object(app, 'recording_store', store):
                app.start_summary_job(record, {'config': {}})
                task = app.background_summary_tasks[record.id]
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0)
                self.assertNotIn(record.id, app.background_summary_tasks)
                self.assertEqual(store.find(record.id).metadata['summary_job']['status'], 'interrupted')

    async def test_websocket_reconnect_reuses_session_with_unfinished_work(self):
        class Socket:
            headers = {"origin": "http://127.0.0.1:8765"}
            url = "ws://127.0.0.1:8765/ws/transcribe"
            def __init__(self):
                self.queue = asyncio.Queue()
                self.events = {}
            async def accept(self): pass
            async def receive(self): return await self.queue.get()
            async def send_json(self, payload): self.events.setdefault(payload['type'], asyncio.Event()).set()
            async def wait_for(self, kind):
                await asyncio.wait_for(self.events.setdefault(kind, asyncio.Event()).wait(), 1)
            def command(self, kind): self.queue.put_nowait({'text': json.dumps({'type': kind})})
            def disconnect(self): self.queue.put_nowait({'type': 'websocket.disconnect'})
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            store = store_for(record)
            manager = SimpleNamespace(status='idle', client_connected=lambda: None,
                                      client_disconnected=lambda: None, load=AsyncMock())
            first, second = Socket(), Socket()
            gate = asyncio.Event()
            with patch.object(app, 'recording_store', store), patch.object(app, 'model_manager', manager):
                connection1 = asyncio.create_task(app.transcribe_socket(first))
                first.command('start')
                await first.wait_for('session_started')
                session = app.live_sessions[record.id]
                session.track(gate.wait())
                first.command('pause')
                await first.wait_for('paused')
                first.disconnect()
                await connection1
                connection2 = asyncio.create_task(app.transcribe_socket(second))
                second.command('start')
                await second.wait_for('session_started')
                self.assertIs(app.live_sessions[record.id], session)
                self.assertTrue(session.tasks)
                self.assertTrue(session.accepting_audio)
                gate.set()
                second.disconnect()
                await connection2
                await session.drain()
                app.live_sessions.pop(record.id, None)

    async def test_end_waits_for_tail_then_generates_even_after_socket_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            store = store_for(record)
            async def closed_socket(payload):
                raise RuntimeError('socket closed')
            session = TranscriptionSession(SimpleNamespace(send_json=closed_socket), None, store)
            session.recording = record
            session.recovered = True
            released = asyncio.Event()
            async def last_segment():
                await released.wait()
                record.upsert_segment({'id': 1, 'start': 0, 'end': 1, 'raw': '最后的重点'})
            session.track(last_segment())
            payload = {'config': {'ai': {'summary_provider': {'model': 'mock', 'api_key': 'secret-test'}}}}
            async def summarize(rec, config):
                self.assertEqual(rec.segments[0]['raw'], '最后的重点')
                return rec.update_note_summary('完整课堂笔记')
            def complete():
                record.metadata['status'] = 'completed'
                record.save_all()
            with patch.object(app, 'recording_store', store), patch.object(record, 'complete', complete), \
                 patch.object(app, 'build_note_summary', side_effect=summarize), \
                 patch.object(app.ai_secret_store, 'update') as save_keys, \
                 patch.object(RecordingRecord, 'make_bundle'):
                await app.begin_recording_end(session, payload)
                finish = app.finalization_tasks[record.id]
                self.assertFalse(finish.done())
                self.assertNotIn('secret-test', record.meta_path.read_text())
                released.set()
                await asyncio.wait_for(finish, 2)
                job = app.background_summary_tasks.get(record.id)
                if job: await asyncio.wait_for(job, 2)
                self.assertTrue(save_keys.called)
            saved = RecordingRecord.load(record.directory)
            self.assertEqual(saved.metadata['status'], 'completed')
            self.assertEqual(saved.metadata['summary_job']['status'], 'completed')
            self.assertIn('完整课堂笔记', saved.ai_summary_path.read_text())
            self.assertNotIn('secret-test', saved.meta_path.read_text())

    async def test_job_survives_request_and_failed_generation_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            record.metadata['status'] = 'completed'
            record.segments = [{'id': 1, 'start': 0, 'end': 1, 'raw': '课堂文字'}]
            record.update_note_summary('旧笔记仍保留')
            store = store_for(record)
            gate = asyncio.Event()
            calls = 0
            async def generate(rec, config):
                nonlocal calls
                calls += 1
                if calls == 1:
                    await gate.wait()
                    raise RuntimeError('上游暂时失败')
                return rec.update_note_summary('重新生成的新笔记')
            with patch.object(app, 'recording_store', store), \
                 patch.object(app, 'build_note_summary', side_effect=generate), \
                 patch.object(RecordingRecord, 'make_bundle'):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                    response = await client.post(f'/api/recordings/{record.id}/summary-jobs', json={'config': {}})
                    self.assertEqual(response.status_code, 200)
                    first = app.background_summary_tasks[record.id]
                    await client.post(f'/api/recordings/{record.id}/summary-jobs', json={'config': {}})
                    self.assertIs(app.background_summary_tasks[record.id], first)
                # Closing the HTTP client must not cancel the server-owned job.
                self.assertFalse(first.done())
                gate.set()
                await first
                saved = store.find(record.id)
                self.assertEqual(saved.metadata['summary_job']['status'], 'failed')
                self.assertEqual(saved.metadata['ai_note_summary']['summary'], '旧笔记仍保留')
                app.start_summary_job(saved, {'config': {}})
                await app.background_summary_tasks[record.id]
            saved = store.find(record.id)
            self.assertEqual(saved.metadata['summary_job']['status'], 'completed')
            self.assertEqual(saved.metadata['ai_note_summary']['summary'], '重新生成的新笔记')

    def test_startup_marks_jobs_interrupted_without_discarding_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            record.metadata.update(summary_job={'status': 'running'}, finalization={'status': 'running'},
                                   ai_note_summary_draft={'fingerprint': 'test', 'parts': {'0': '成功的部分'}})
            record.save_metadata()
            store = store_for(record)
            store.active = None
            store.refresh(mark_interrupted=True)
            saved = store.find(record.id)
            self.assertEqual(saved.metadata['summary_job']['status'], 'interrupted')
            self.assertEqual(saved.metadata['finalization']['status'], 'interrupted')
            self.assertEqual(saved.metadata['ai_note_summary_draft']['parts']['0'], '成功的部分')


class FullContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_lecture_goes_in_one_request_and_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            record = live_record(Path(directory))
            record.segments = [{'id': n, 'start': n * 60, 'end': (n + 1) * 60,
                                'raw': f'第{n}节 ' + '公式推导。' * 1000} for n in range(5)]
            config = {'ai': {'summary_provider': {'context_window': 256000}}}
            answer = '## 知识点速览\n跨章节联系\n## 按课堂时间展开\n完整讲义'
            async def summarize(text, previous, options, **kwargs):
                self.assertTrue(options['_full_note'])
                self.assertFalse(options['_detailed_chunk'])
                for n in range(5): self.assertIn(f'第{n}节', text)
                return {'summary': answer}
            with patch('qwen_asr_app.batch_ai.summarize_transcript', side_effect=summarize) as mocked, \
                 patch('qwen_asr_app.batch_ai.summarize_note_abstract', return_value='课堂摘要'):
                result = await build_note_summary(record, config)
            self.assertEqual(mocked.await_count, 1)
            self.assertEqual(result['input_mode'], 'full')
            self.assertEqual(result['parts'], 1)
            self.assertTrue(result['summary'].endswith(answer))

    def test_overflow_splits_without_dropping_source_and_reserves_real_payload(self):
        blocks = [f'段{n} ' + '推导\\\"\n' * 2000 for n in range(4)]
        config = {'_note_summary_system_prompt': '完整整理课堂',
                  'ai': {'summary_provider': {'context_window': 8192}}}
        chunks = note_summary_chunks(blocks, config)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks).replace('\n', '').replace(' ', ''),
                         ''.join(blocks).replace('\n', '').replace(' ', ''))
        for chunk in chunks:
            payload = json.dumps({'previous_cumulative_summary': None, 'new_transcript': chunk}, ensure_ascii=False)
            cost = estimate_tokens(summary_instructions({**config, '_detailed_chunk': True}))
            self.assertLessEqual(cost + estimate_tokens(payload) + 4096 + 320, 8192)
