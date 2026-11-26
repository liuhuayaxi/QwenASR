from tests import isolation  # Must precede application imports.
import asyncio
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import av
import numpy as np
from fastapi import FastAPI, HTTPException

from qwen_asr_app.course_download import _stream_audio_inline as stream_audio
from qwen_asr_app.course_stream import CourseStreamJob, CourseStreamManager, course_stream_router
from qwen_asr_app.smartclass import SmartClassClient, CAS, connection_error
from qwen_asr_app.storage import RecordingRecord, RecordingStore


def make_record(directory):
    record = RecordingRecord.create_live("课程测试", "测试", directory)
    record.metadata.update(status="processing", kind="upload", source_filename="smartclass_test.flac",
                           course_stream={"chunks": [], "completed_chunks": 0, "expected_seconds": 6})
    record.save_all()
    return record


async def until(predicate):
    for _ in range(200):
        if predicate(): return
        await asyncio.sleep(.01)
    raise AssertionError("condition not reached")


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_failure_keeps_later_chunks_local(self):
        with tempfile.TemporaryDirectory() as temp:
            record = make_record(Path(temp))
            providers = []
            async def transcribe(audio, language, context, **kwargs):
                provider = kwargs['inference_config']['asr_provider']
                providers.append(provider)
                fallback = provider != 'local'
                if fallback:
                    kwargs['on_provider']({'provider': 'remote', 'fallback': False})
                kwargs['on_provider']({'provider': 'local', 'fallback': fallback})
                kwargs['metrics'].update(provider='local', fallback=fallback)
                if fallback:
                    kwargs['metrics']['fallback_reason'] = 'ProvenNotAccepted'
                return '课堂内容', 'Chinese', .1
            model = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                    transcribe=AsyncMock(side_effect=transcribe))
            config = {'inference_settings': {'asr_provider': 'remote_preferred'}}
            job = CourseStreamJob(record, config, model, set())
            job.start()
            for index in range(3):
                await job.accept(index, bytes(16000*2), {})
            await job.finish_input(3)
            with patch.object(job, 'assemble'):
                await asyncio.wait_for(job.task, 5)
            self.assertEqual(providers, ['remote_preferred', 'local', 'local'])
            self.assertEqual(record.metadata['status'], 'completed')
            self.assertTrue(job.state['asr_local_fallback'])
            self.assertTrue(job.state['asr_execution']['fallback'])
            self.assertTrue(all(s['metrics']['fallback'] for s in record.segments))
            self.assertTrue(all(s['metrics']['fallback_reason'] == 'ProvenNotAccepted' for s in record.segments))
            self.assertEqual(job.config['inference_settings']['asr_provider'], 'remote_preferred')

    async def test_fallback_survives_recovery_and_only_pause_continue_retries_remote(self):
        with tempfile.TemporaryDirectory() as temp:
            record = make_record(Path(temp))
            providers = []
            async def transcribe(audio, language, context, **kwargs):
                provider = kwargs['inference_config']['asr_provider']
                providers.append(provider)
                fallback = len(providers) == 1
                actual = 'local' if fallback or provider == 'local' else 'remote'
                kwargs['on_provider']({'provider': actual, 'fallback': fallback})
                kwargs['metrics'].update(provider=actual, fallback=fallback)
                return '已保存的课堂内容', 'Chinese', .1
            model = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                    transcribe=AsyncMock(side_effect=transcribe))
            config = {'inference_settings': {'asr_provider': 'remote_preferred'}}
            job = CourseStreamJob(record, config, model, set())
            job.start()
            await job.accept(0, bytes(16000*2), {})
            await until(lambda: job.state.get('completed_chunks') == 1)
            await job.stop()  # Process recovery is not a user pause.
            loaded = RecordingRecord.load(record.directory)
            recovered = CourseStreamJob(loaded, config, model, set())
            recovered.start()
            await recovered.accept(1, bytes(16000*2), {})
            await until(lambda: recovered.state.get('completed_chunks') == 2)
            self.assertEqual(providers, ['remote_preferred', 'local'])
            await recovered.stop(paused=True)
            loaded = RecordingRecord.load(record.directory)
            resumed = CourseStreamJob(loaded, config, model, set())
            resumed.start()
            self.assertFalse(resumed.state['asr_local_fallback'])
            await resumed.accept(2, bytes(16000*2), {})
            await resumed.finish_input(3)
            with patch.object(resumed, 'assemble'):
                await asyncio.wait_for(resumed.task, 5)
            self.assertEqual(providers, ['remote_preferred', 'local', 'remote_preferred'])
            self.assertEqual(len(loaded.segments), 3)
            self.assertEqual(resumed.state['asr_execution']['provider'], 'remote')
            self.assertFalse(resumed.state['asr_execution']['fallback'])

    async def test_unresolved_remote_error_does_not_recompute_accepted_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            record = make_record(Path(temp))
            model = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                    transcribe=AsyncMock(side_effect=RuntimeError('远端操作等待恢复')))
            job = CourseStreamJob(record, {'inference_settings': {'asr_provider': 'remote_preferred'}}, model, set())
            job.start()
            await job.accept(0, bytes(16000*2), {})
            await asyncio.wait_for(job.task, 5)
            self.assertEqual(record.metadata['status'], 'interrupted')
            self.assertFalse(job.state.get('asr_local_fallback'))
            self.assertEqual(job.state['completed_chunks'], 0)
            self.assertEqual(model.transcribe.await_count, 1)
            self.assertTrue((record.directory / '.course-chunks' / '00000.pcm').exists())

    def test_library_item_keeps_resume_protocol_without_large_private_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp:
            record = make_record(Path(temp))
            record.metadata['course_stream']['chunks'] = [{'large_checkpoint': 'x' * 5000}]
            record.metadata['course_stream']['inference_settings'] = {'token': 'private-fixture'}
            value = record.list_item()
            self.assertIs(value['course_stream'], True)
            self.assertEqual(value['source_filename'], 'smartclass_test.flac')
            self.assertNotIn('private-fixture', str(value))
            self.assertNotIn('large_checkpoint', str(value))

    async def test_legacy_retranscription_preserves_aac_after_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            record = make_record(Path(temp))
            record.metadata.pop('working_audio_filename')
            record.metadata['recorded_at'] = '2026-09-10 13:45'
            record.metadata['started_at'] = '2026-09-10T16:57:44+08:00'
            record.save_all()
            record = RecordingRecord.load(record.directory)
            self.assertEqual(record.working_audio_path, record.audio_path)
            record.audio_path.write_bytes(b'old archive to replace')
            model = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                    transcribe=AsyncMock(return_value=('重新转写', 'Chinese', .1)))
            job = CourseStreamJob(record, {}, model, set())
            job.start()
            await job.accept(0, (np.ones(3*16000, dtype='<i2') * 500).tobytes(), {'source': 1, 'samples': 0})
            await job.finish_input(1)
            await asyncio.wait_for(job.task, 5)
            loaded = RecordingRecord.load(record.directory)
            self.assertEqual(loaded.metadata['status'], 'completed')
            self.assertTrue(loaded.audio_path.exists())
            self.assertFalse(loaded.working_audio_path.exists())
            self.assertNotEqual(loaded.audio_path, loaded.working_audio_path)
            self.assertEqual(loaded.metadata['recorded_at'], '2026-09-10T13:45:00+08:00')
            self.assertEqual(loaded.metadata['started_at'], '2026-09-10T13:45:00+08:00')
            self.assertEqual(loaded.metadata['imported_at'], '2026-09-10T16:57:44+08:00')
            with av.open(str(loaded.audio_path)) as audio:
                self.assertEqual(audio.streams.audio[0].codec_context.name, 'aac')
                self.assertGreaterEqual(sum(frame.samples for frame in audio.decode(audio=0)), 3*16000)

    async def test_download_asr_and_correction_overlap_and_one_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            record = make_record(Path(temp))
            release_ai = asyncio.Event()
            started_ai = asyncio.Event()
            async def correct(record, segment, config):
                started_ai.set()
                await release_ai.wait()
                segment.update(corrected="精修：" + segment["raw"], ai_pending=False)
                record.upsert_segment(segment)
                return segment
            model = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                    transcribe=AsyncMock(return_value=("课堂内容", "Chinese", .1)))
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', correct):
                job = CourseStreamJob(record, {"ai_correction": True}, model, set())
                job.start()
                block = (np.ones(3*16000, dtype='<i2') * 500).tobytes()
                await job.accept(0, block, {"source": 0, "samples": 48000})
                await asyncio.wait_for(started_ai.wait(), 2)
                self.assertFalse(job.state.get("input_complete"))
                self.assertEqual(len(record.segments), 1)
                self.assertFalse(record.segments[0].get("corrected"))
                await job.accept(1, block, {"source": 1, "samples": 0})
                await until(lambda: model.transcribe.await_count == 2)
                self.assertFalse(release_ai.is_set())  # Next ASR runs while first AI is pending.
                await job.accept(1, block, {"source": 1, "samples": 0})
                self.assertEqual(len(job.state["chunks"]), 2)
                with self.assertRaises(HTTPException):
                    await job.accept(1, bytes(len(block)), {})
                await job.finish_input(2)
                release_ai.set()
                await asyncio.wait_for(job.task, 5)
            self.assertEqual(record.metadata['status'], 'completed')
            self.assertEqual(record.duration, 6)
            self.assertEqual([s['start'] for s in record.segments], [0, 3])
            self.assertTrue(all(s.get('corrected') for s in record.segments))
            self.assertTrue(record.audio_path.exists())
            self.assertEqual(len(list(Path(temp).rglob('session.json'))), 1)
            self.assertEqual(model.active_jobs, 0)

    async def test_resume_saved_corrections_uses_first_round_without_asr_or_second_ai_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            record=make_record(Path(temp))
            raw=bytes(3*16000*2)
            import hashlib
            record.segments=[{'id':101,'raw':'原始数值九','start':0,'end':3,'course_chunk':0,'chunk_end_sample':48000}]
            record.metadata['ai_reviews']=[{'id':'saved','segment_ids':[101],'raw':'原始数值九','corrected':'原始数值九','start':0,'end':3,
                'correction_review':{'before':'原始数值九','after':'原始数值9','risks':[],'evidence':[]}}]
            record.metadata['course_stream'].update(chunks=[{'samples':48000,'sha256':hashlib.sha256(raw).hexdigest(),'cursor':{}}],completed_chunks=1,input_complete=True)
            folder=record.directory/'.course-chunks';folder.mkdir();(folder/'00000.pcm').write_bytes(raw)
            model=SimpleNamespace(active_jobs=0,cancel_unload=lambda:None,schedule_unload=lambda:None,transcribe=AsyncMock())
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment',new=AsyncMock()) as rewrite,patch('qwen_asr_app.transcription_assist.verification.verify_corrections',new=AsyncMock()) as verify:
                job=CourseStreamJob(record,{'ai_correction':True},model,set());job.start()
                await asyncio.wait_for(job.task,5)
            rewrite.assert_not_awaited();model.transcribe.assert_not_awaited();verify.assert_not_awaited()
            self.assertEqual(record.metadata['status'],'completed')
            self.assertEqual(record.segments[0]['raw'],'原始数值九')
            from qwen_asr_app.note_content import transcript_blocks
            self.assertEqual(transcript_blocks(record)[0]['text'],'原始数值9')

    async def test_pipeline_completion_does_not_retry_a_removed_transcript_review(self):
        from qwen_asr_app.ai import AIRequestError
        from qwen_asr_app.batch_pipeline import SegmentCorrectionPipeline
        with tempfile.TemporaryDirectory() as temp:
            record=make_record(Path(temp))
            record.segments=[{'id':101,'raw':'已保存原文','corrected':'已保存原文','start':0,'end':3}]
            with patch('qwen_asr_app.transcription_assist.verification.verify_corrections',
                       new=AsyncMock(side_effect=[AIRequestError('interrupted',kind='network_transport'), {'checked':1}])) as verify, \
                 patch('qwen_asr_app.ai.asyncio.sleep',new=AsyncMock()), \
                 patch('qwen_asr_app.batch_pipeline.correct_batch_segment',new=AsyncMock()) as rewrite:
                pipeline=SegmentCorrectionPipeline(record, {})
                await asyncio.wait_for(pipeline.finish(),2)
            verify.assert_not_awaited()
            rewrite.assert_not_awaited()
            self.assertEqual(record.segments[0]['raw'],'已保存原文')
            self.assertNotIn('correction_verification',record.metadata)

    async def test_resume_accepted_chunks_without_duplicate_asr(self):
        with tempfile.TemporaryDirectory() as temp:
            record = make_record(Path(temp))
            model = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                    transcribe=AsyncMock(return_value=("已转写", "Chinese", .1)))
            job = CourseStreamJob(record, {}, model, set()); job.start()
            block = bytes(3*16000*2)
            await job.accept(0, block, {"source": 0, "samples": 48000})
            await until(lambda: job.state.get('completed_chunks') == 1)
            await job.stop()
            loaded = RecordingRecord.load(record.directory)
            resumed = CourseStreamJob(loaded, {}, model, set()); resumed.start()
            self.assertEqual(resumed.snapshot()['resume']['samples'], 48000)
            await resumed.accept(1, block, {"source": 1, "samples": 0})
            await resumed.finish_input(2)
            await asyncio.wait_for(resumed.task, 5)
            self.assertEqual(model.transcribe.await_count, 2)
            self.assertEqual(len(loaded.segments), 2)


class DownloadTests(unittest.TestCase):
    def test_pcm_integrity_and_seek_resume_at_acknowledged_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.wav'
            pcm = (np.sin(np.arange(90*16000)*.07)*12000).astype('<i2')
            with wave.open(str(path), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(pcm.tobytes())
            sources = [dict(url=str(path), duration=90, video_id='v', segment_id=1, title='课次', start='2026-09-10')]
            chunks = []
            real_open = av.open
            with patch('qwen_asr_app.course_download.media_url', lambda x:x), patch('qwen_asr_app.course_download.av.open', lambda url, **kwargs: real_open(url)):
                stream_audio(sources, lambda raw,cursor:chunks.append((raw,cursor)))
                self.assertGreater(len(chunks), 1)
                self.assertEqual(b''.join(raw for raw,_ in chunks), pcm.tobytes())
                resumed = []
                stream_audio(sources, lambda raw,cursor:resumed.append(raw), resume=chunks[0][1])
                self.assertEqual(chunks[0][0] + b''.join(resumed), pcm.tobytes())


class ConnectionTests(unittest.TestCase):
    def test_get_retries_but_password_post_is_never_replayed(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            if len(calls) == 1: raise httpx.ConnectError('network changed', request=request)
            return httpx.Response(200, text='ok')
        c = SmartClassClient(transport=httpx.MockTransport(handler))
        with patch('qwen_asr_app.smartclass.time.sleep'):
            self.assertEqual(c.request('GET', CAS+'/cas/login').text, 'ok')
        self.assertEqual(calls, ['GET','GET']); calls.clear()
        with self.assertRaises(httpx.ConnectError):
            c.request('POST', CAS+'/ssoLogin/login', json={'password':'secret'})
        self.assertEqual(calls, ['POST']); c.close()

    def test_error_reports_stage_and_host_without_query_credentials(self):
        e = httpx.ReadTimeout('private detail', request=httpx.Request('GET', CAS+'/path?ticket=secret'))
        message = connection_error(e, '建立平台会话')
        self.assertIn('建立平台会话', message)
        self.assertIn('响应超时', message)
        self.assertNotIn('secret', message)


if __name__ == '__main__': unittest.main()
