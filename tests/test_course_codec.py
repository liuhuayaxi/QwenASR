from tests import isolation
import contextlib
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from qwen_asr_app.course_codec import _codec_worker, run_codec
from qwen_asr_app.smartclass import SmartClassError


def local_codec_worker(connection, request):
    from qwen_asr_app import course_download
    @contextlib.contextmanager
    def local_media(url, stop):
        with open(request['sources'][0]['test_path'], 'rb') as stream:
            yield stream
    course_download.parallel_media = local_media
    _codec_worker(connection, request)


def stalled_worker(connection, request):
    time.sleep(60)


def crashed_worker(connection, request):
    os._exit(7)


class ReplayCodecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'audio.wav'
        self.pcm = b'\x01\x00' * (100 * 16000)
        with wave.open(str(self.path), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(self.pcm)
        self.sources = [dict(url='https://njuptvod7.smartclass.cn:8010/test.mp4',
            duration=100, video_id='test', segment_id=1, title='test', start='2026-01-01',
            test_path=str(self.path))]
        self.before = {p.pid for p in multiprocessing.active_children()}

    def tearDown(self):
        self.assertEqual({p.pid for p in multiprocessing.active_children()} - self.before, set())

    def test_real_codec_checkpoint_and_resume_are_sample_exact(self):
        chunks, resumed, progress = [], [], []
        with patch('qwen_asr_app.course_codec._codec_worker', local_codec_worker):
            result = run_codec('stream', self.sources, on_chunk=lambda raw, cursor: chunks.append((raw, cursor)),
                               progress=lambda *values: progress.append(values))
            run_codec('stream', self.sources, on_chunk=lambda raw, cursor: resumed.append(raw), resume=chunks[0][1])
        self.assertEqual(b''.join(raw for raw, _ in chunks), self.pcm)
        self.assertEqual(chunks[0][0] + b''.join(resumed), self.pcm)
        self.assertEqual(result[0]['duration_seconds'], 100)
        self.assertTrue(progress)

    def test_callback_failure_reaps_worker_without_acknowledging_chunk(self):
        def fail(raw, cursor):
            raise ValueError('checkpoint write failed')
        with patch('qwen_asr_app.course_codec._codec_worker', local_codec_worker):
            with self.assertRaisesRegex(ValueError, 'checkpoint'):
                run_codec('stream', self.sources, on_chunk=fail)

    def test_stop_interrupts_blocked_network_decoder(self):
        stop = threading.Event()
        timer = threading.Timer(.3, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch('qwen_asr_app.course_codec._codec_worker', stalled_worker):
                with self.assertRaises(SmartClassError):
                    run_codec('stream', self.sources, stop=stop)
        finally:
            timer.cancel();timer.join()
        self.assertLess(time.monotonic() - started, 4)

    def test_worker_crash_does_not_hang(self):
        with patch('qwen_asr_app.course_codec._codec_worker', crashed_worker):
            with self.assertRaises(SmartClassError):
                run_codec('stream', self.sources)

    def test_pre_stopped_task_never_spawns(self):
        stop = threading.Event();stop.set()
        with patch('qwen_asr_app.course_codec.mp.get_context') as context:
            with self.assertRaises(SmartClassError):
                run_codec('stream', self.sources, stop=stop)
            context.assert_not_called()
