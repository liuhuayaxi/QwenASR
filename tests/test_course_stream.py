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

from qwen_asr_app.course_download import stream_audio
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
