from tests import isolation  # noqa: F401

import asyncio
import json
from pathlib import Path
import tempfile
import time
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from PIL import Image

from qwen_asr_app.course_file_routes import CourseFileAPI
from qwen_asr_app.native_pdf import pdf_operation
from qwen_asr_app.vga_jobs import VGAJobs
from qwen_asr_app.vga_slides import ExtractionCancelled
from tests.test_vga_slides import synthetic_video


class VGAJobRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = SimpleNamespace(storage_root=self.root / 'library', categories=['课程'],
                                        library_layout='shared-courses-v1')
        private = patch('qwen_asr_app.vga_jobs.PRIVATE_ROOT', self.root / 'private')
        private.start()
        self.addCleanup(private.stop)
        self.api = CourseFileAPI(self.settings, None, None, asyncio.Lock())
        service = FastAPI()
        service.include_router(self.api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://test',
                                       headers={'X-QwenASR-Client': 'course-files', 'Origin': 'http://test'})
        self.addAsyncCleanup(self.client.aclose)
        self.video = self.root / 'lecture.mp4'
        synthetic_video(self.video)

    async def start(self, **overrides):
        return await self.client.post('/api/course-files/from-vga', json={
            'category': '课程', 'video_path': str(self.video), **overrides})

    async def finish(self, ident):
        await asyncio.gather(*list(self.api.tasks.values()))
        response = await self.client.get('/api/course-files/from-vga/' + ident)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_extract_import_manifest_and_recover_status(self):
        response = await self.start(display_name='合成课堂')
        self.assertEqual(response.status_code, 200, response.text)
        ident = response.json()['id']
        job = await self.finish(ident)
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(job['progress'], 100)
        self.assertEqual(job['pages'], 3)
        items = self.api.store.list('课程')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['kind'], '课件')
        self.assertEqual(items[0]['name'], '合成课堂(VGA课件).pdf')
        _, path = self.api.store.file('课程', job['asset_id'])
        self.assertEqual(pdf_operation('page_count', path=str(path)), 3)
        manifest = json.loads((self.api.vga_jobs.root / (job['asset_id'] + '.json')).read_text())
        self.assertEqual(manifest['pages'][1]['captured_sec'], 8)
        latest = await self.client.get('/api/course-files/from-vga', params={'category': '课程'})
        self.assertEqual(latest.json()['job']['id'], ident)
        restored = VGAJobs(self.api.store, self.api.lock, {}, root=self.api.vga_jobs.root)
        self.assertEqual(restored.get(ident)['status'], 'completed')

    async def test_invalid_paths_video_category_and_guard(self):
        for overrides in ({'video_path': ''}, {'video_path': str(self.root)},
                          {'video_path': str(self.root / 'missing.mp4')}, {'category': '不存在'}):
            response = await self.start(**overrides)
            self.assertEqual(response.status_code, 400, response.text)
        invalid = self.root / 'bad.mp4'
        invalid.write_bytes(b'not a video')
        self.assertEqual((await self.start(video_path=str(invalid))).status_code, 400)
        import wave
        audio = self.root / 'audio.wav'
        with wave.open(str(audio), 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\0\0' * 16000)
        self.assertEqual((await self.start(video_path=str(audio))).status_code, 400)
        denied = await self.client.post('/api/course-files/from-vga', json={}, headers={'X-QwenASR-Client': 'other'})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual((await self.client.get('/api/course-files/from-vga/missing')).status_code, 404)
        self.assertEqual(self.api.store.list('课程'), [])

    async def test_blank_video_fails_without_import(self):
        synthetic_video(self.video, [(Image.new('RGB', (160, 120), 'black'), 2)])
        response = await self.start()
        job = await self.finish(response.json()['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('未找到稳定课件画面', job['error'])
        self.assertEqual(self.api.store.list('课程'), [])

    async def test_cancel_conflict_and_shutdown(self):
        entered = Event()

        def blocking_extract(*args, cancel, **kwargs):
            entered.set()
            while not cancel():
                time.sleep(.01)
            raise ExtractionCancelled()

        with patch('qwen_asr_app.vga_slides.extract_slides', side_effect=blocking_extract):
            response = await self.start()
            ident = response.json()['id']
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertEqual((await self.start()).status_code, 409)
            cancelled = await self.client.post(f'/api/course-files/from-vga/{ident}/cancel')
            self.assertEqual(cancelled.json()['status'], 'stopping')
            self.assertEqual((await self.finish(ident))['status'], 'cancelled')
            response = await self.start()
            ident = response.json()['id']
            await asyncio.sleep(.02)
            task = next(iter(self.api.tasks.values()))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(self.api.vga_jobs.get(ident)['status'], 'interrupted')
        self.assertEqual(self.api.store.list('课程'), [])
