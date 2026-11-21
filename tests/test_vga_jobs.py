from tests import isolation  # noqa: F401

import asyncio
import json
from pathlib import Path
import tempfile
import time
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
from PIL import Image

from qwen_asr_app.course_file_routes import CourseFileAPI
from qwen_asr_app.native_pdf import pdf_operation
from qwen_asr_app.vga_jobs import VGAJobs
from qwen_asr_app.vga_slides import ExtractionCancelled
from tests.test_vga_slides import synthetic_video
from tests.test_vga_filter import fixture


class VGAJobRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
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

    async def test_filter_switches(self):
        from qwen_asr_app.vga_slides import Slide

        slides = []
        for index, kind in enumerate(('slide', 'document', 'junk'), 1):
            path = self.root / f'page-{index}.png'
            fixture(kind).save(path)
            slides.append(Slide(index, index * 3, index * 3 + 2, index * 3 + 2, path))
        for options, count in (({}, 2), ({'keep_homework': False}, 1),
                               ({'filter_enabled': False, 'keep_homework': False}, 3)):
            with patch('qwen_asr_app.vga_slides.extract_slides', return_value=slides):
                response = await self.start(display_name=str(count), **options)
                self.assertEqual(response.status_code, 200, response.text)
                job = await self.finish(response.json()['id'])
            self.assertEqual(job['status'], 'completed', job)
            self.assertEqual(job['pages'], count)
            self.assertEqual(job['filter_enabled'], options.get('filter_enabled', True))
            manifest = json.loads((self.api.vga_jobs.root / (job['asset_id'] + '.json')).read_text())
            self.assertEqual(len(manifest['pages']), count)
            self.assertTrue(all('kind' in page and 'section' in page for page in manifest['pages']))
        self.assertEqual((await self.start(filter_enabled='false')).status_code, 400)
        self.assertEqual((await self.start(keep_homework=0)).status_code, 400)

    async def test_filtered_junk_fails_without_import(self):
        from qwen_asr_app.vga_slides import Slide

        path = self.root / 'desktop.png'
        fixture('junk').save(path)
        with patch('qwen_asr_app.vga_slides.extract_slides', return_value=[Slide(1, 0, 2, 2, path)]):
            response = await self.start()
            job = await self.finish(response.json()['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('过滤后没有', job['error'])
        self.assertEqual(self.api.store.list('课程'), [])

    async def test_emphasis_switch_audio_and_failure_warning(self):
        segments = [{'start': 1, 'end': 2, 'raw': '这是考试重点'}]
        with patch('qwen_asr_app.vga_emphasis.find_audio', return_value=self.video) as find, \
             patch('qwen_asr_app.vga_emphasis.transcribe_audio', new_callable=AsyncMock) as transcribe:
            transcribe.return_value = (segments, 'recording-test')
            response = await self.start(audio_path='/录音.aac')
            self.assertTrue(response.json()['emphasis_enabled'])
            job = await self.finish(response.json()['id'])
            self.assertEqual(job['recording_id'], 'recording-test')
            find.assert_called_with(str(self.video), '/录音.aac')
            self.assertEqual(transcribe.call_args.args[1], '课程')
            manifest = json.loads((self.api.vga_jobs.root / (job['asset_id'] + '.json')).read_text())
            self.assertTrue(manifest['pages'][0]['important'])
            self.assertEqual(manifest['pages'][0]['transcript_text'], '这是考试重点')
            transcribe.reset_mock()
            response = await self.start(emphasis_enabled=False)
            job = await self.finish(response.json()['id'])
            self.assertEqual(job['status'], 'completed')
            transcribe.assert_not_called()
            transcribe.side_effect = RuntimeError('语音识别暂不可用')
            response = await self.start()
            job = await self.finish(response.json()['id'])
            self.assertEqual(job['status'], 'completed')
            self.assertEqual(job['pages'], 3)
            self.assertIn('重点标记失败', job['warnings'][0])
        for options in ({'emphasis_enabled': 1}, {'audio_path': 1}):
            self.assertEqual((await self.start(**options)).status_code, 400)

    async def test_filter_disabled_marks_only_revisit_keyword(self):
        from qwen_asr_app.vga_slides import extract_slides

        revisit = extract_slides(self.video, deduplicate=False)[-1]
        segments = [{'start': revisit.start_sec + .1, 'end': revisit.end_sec, 'raw': '考试重点'}]
        with patch('qwen_asr_app.vga_emphasis.find_audio', return_value=self.video), \
             patch('qwen_asr_app.vga_emphasis.transcribe_audio', new_callable=AsyncMock,
                   return_value=(segments, 'recording-revisit')):
            response = await self.start(filter_enabled=False)
            job = await self.finish(response.json()['id'])
        self.assertEqual(job['status'], 'completed')
        manifest = json.loads((self.api.vga_jobs.root / (job['asset_id'] + '.json')).read_text())
        self.assertEqual(len(manifest['pages']), 3)
        self.assertTrue(manifest['pages'][0]['important'])
        self.assertEqual(len(manifest['pages'][0]['occurrences']), 2)

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
