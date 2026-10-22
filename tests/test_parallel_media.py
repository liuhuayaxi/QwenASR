from tests import isolation

import io
import re
import threading
import unittest
import wave
from unittest.mock import patch

import httpx
import numpy as np

from qwen_asr_app.course_download import _stream_audio_inline as stream_audio
from qwen_asr_app.parallel_media import BLOCK_BYTES, PROBE_BYTES, parallel_media
from qwen_asr_app.smartclass import SmartClassError

URL = 'https://njuptvod7.smartclass.cn:8010/test.mp4'


class RangeTests(unittest.TestCase):
    def client_patch(self, handler):
        real = httpx.Client
        return patch('qwen_asr_app.parallel_media.httpx.Client',
                     side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    def test_four_connections_prioritized_seek_and_exact_bytes(self):
        data = bytes(range(256)) * (6 * BLOCK_BYTES // 256)
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = 0
        peak = 0
        calls = []

        def handler(request):
            nonlocal active, peak
            start, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.headers['range']).groups())
            end = min(end, len(data) - 1)
            self.assertNotIn('cookie', request.headers)
            self.assertNotIn('authorization', request.headers)
            with lock:
                calls.append(start)
                active += 1
                peak = max(peak, active)
            if start in {PROBE_BYTES, BLOCK_BYTES, 2 * BLOCK_BYTES, 3 * BLOCK_BYTES}:
                barrier.wait(timeout=2)
            with lock:
                active -= 1
            return httpx.Response(206, content=data[start:end+1], headers={
                'content-range': f'bytes {start}-{end}/{len(data)}', 'etag': '"stable"'})

        with self.client_patch(handler), parallel_media(URL, threading.Event()) as media:
            self.assertEqual(media.read(123), data[:123])
            self.assertEqual(media.seek(-100, io.SEEK_END), len(data) - 100)
            self.assertEqual(media.read(100), data[-100:])
            self.assertEqual(media.read(10), b'')
            media.seek(0)
            received = bytearray()
            while block := media.read(32768):
                received.extend(block)
            self.assertEqual(received, data)
        self.assertEqual(peak, 4)
        self.assertEqual(len(calls), 7)
        self.assertTrue(media.closed)
        self.assertTrue(all(not worker.is_alive() for worker in media.threads))

    def test_parallel_decoder_and_resume_have_no_lost_or_duplicate_samples(self):
        pcm = (np.sin(np.arange(100 * 16000) * .07) * 12000).astype('<i2')
        file = io.BytesIO()
        with wave.open(file, 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(pcm.tobytes())
        data = file.getvalue()

        def handler(request):
            start, end = map(int, request.headers['range'][6:].split('-'))
            end = min(end, len(data)-1)
            return httpx.Response(206, content=data[start:end+1],
                                  headers={'content-range': f'bytes {start}-{end}/{len(data)}'})

        sources = [dict(url=URL, duration=100, video_id='v', segment_id=1, title='课次', start='2026-09-10')]
        chunks, resumed = [], []
        with self.client_patch(handler):
            manifest = stream_audio(sources, lambda raw, cursor: chunks.append((raw, cursor)))
            stream_audio(sources, lambda raw, cursor: resumed.append(raw), resume=chunks[0][1])
        self.assertEqual(b''.join(raw for raw, _ in chunks), pcm.tobytes())
        self.assertEqual(chunks[0][0] + b''.join(resumed), pcm.tobytes())
        self.assertEqual(manifest[0]['duration_seconds'], 100)

    def test_no_range_falls_back_without_reading_entire_body(self):
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError('Unsupported range must not consume a video')
        with self.client_patch(lambda request: httpx.Response(200, stream=Body())), \
                parallel_media(URL, threading.Event()) as media:
            self.assertEqual(media, URL)

    def test_corrupt_range_and_pause_fail_cleanly_and_join_workers(self):
        stop = threading.Event()
        def handler(request):
            if request.headers['range'].startswith('bytes=0-'):
                return httpx.Response(206, content=bytes(PROBE_BYTES),
                                      headers={'content-range': f'bytes 0-{PROBE_BYTES-1}/{2*BLOCK_BYTES}'})
            return httpx.Response(200, content=b'wrong response')
        with self.client_patch(handler), parallel_media(URL, stop) as media:
            media.seek(BLOCK_BYTES)
            with self.assertRaises(SmartClassError):
                media.read(100)
        self.assertTrue(all(not worker.is_alive() for worker in media.threads))
        with self.client_patch(handler), parallel_media(URL, stop) as media:
            stop.set()
            with self.assertRaises(SmartClassError):
                media.read(100)


if __name__ == '__main__':
    unittest.main()
