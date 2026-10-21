from tests import isolation  # noqa: F401

import asyncio
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from qwen_asr_app.course_file_routes import CourseFileAPI
from qwen_asr_app.note_library import NoteLibrary, note_library_router
from qwen_asr_app.pdf_preview import open_pdf_in_chrome


class ChromePdfTests(unittest.TestCase):
    def test_open_passes_validated_path_as_one_argument_without_shell(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / '课程 名称 $(literal).pdf'
            path.write_bytes(b'%PDF-fixture')
            with patch('qwen_asr_app.pdf_preview.sys.platform', 'darwin'), \
                 patch('qwen_asr_app.pdf_preview.subprocess.run') as run:
                result = open_pdf_in_chrome(path)
            self.assertTrue(result['opened'])
            self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-a', 'Google Chrome', str(path.resolve())])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_non_pdf_missing_file_and_chrome_error_do_not_report_success(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'note.txt'
            path.write_text('fixture')
            with patch('qwen_asr_app.pdf_preview.subprocess.run') as run:
                with self.assertRaises(ValueError): open_pdf_in_chrome(path)
                with self.assertRaises(OSError): open_pdf_in_chrome(Path(raw) / 'absent.pdf')
                run.assert_not_called()
            pdf = path.with_suffix('.pdf')
            pdf.write_bytes(b'%PDF-fixture')
            with patch('qwen_asr_app.pdf_preview.sys.platform', 'darwin'), \
                 patch('qwen_asr_app.pdf_preview.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'open')):
                with self.assertRaisesRegex(ValueError, 'Chrome'):
                    open_pdf_in_chrome(pdf)


class PdfPreviewRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_course_original_and_converted_resolve_from_material_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            settings = SimpleNamespace(storage_root=root, categories=['课程'], library_layout='shared-courses-v1')
            api = CourseFileAPI(settings, None, None, asyncio.Lock())
            original = root / 'upload.pdf'
            original.write_bytes(b'%PDF-fixture')
            item, _ = api.store.import_file('课程', original, '讲义.pdf', '讲义')
            _, original = api.store.file('课程', item['id'])
            derived = api.store.shared_store.output_path('课程', item)
            derived.parent.mkdir(parents=True, exist_ok=True)
            derived.write_bytes(b'%PDF-converted')
            api.store.shared_store.cache.clear()
            service = FastAPI()
            service.include_router(api.router)
            headers = {'X-QwenASR-Client': 'course-files', 'Origin': 'http://test'}
            with patch('qwen_asr_app.pdf_preview.open_pdf_in_chrome', return_value={'opened': True, 'browser': 'chrome'}) as opened:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://test') as client:
                    denied = await client.post(f'/api/course-files/{item["id"]}/open-pdf', json={'category': '课程'})
                    self.assertEqual(denied.status_code, 403)
                    for variant, path in [('original', original), ('pdf', derived)]:
                        response = await client.post(f'/api/course-files/{item["id"]}/open-pdf', headers=headers, json={'category': '课程', 'variant': variant})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(opened.call_args.args, (path,))
                    missing = await client.post('/api/course-files/invalid/open-pdf', headers=headers, json={'category': '课程'})
                    self.assertEqual(missing.status_code, 400)

    async def test_external_review_pdf_title_uses_its_existing_catalog(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            settings = SimpleNamespace(storage_root=root / 'library', categories=['课程'])
            recordings = SimpleNamespace(list_records=lambda: [])
            with patch('qwen_asr_app.note_library.NOTE_STATE_PATH', root / 'catalog.json'):
                library = NoteLibrary(settings, recordings)
            upload = root / 'review.pdf'
            upload.write_bytes(b'%PDF-review')
            note = library.add('课程', '外部复习', upload.name, [], upload)
            service = FastAPI()
            service.include_router(note_library_router(settings, recordings, library=library))
            with patch('qwen_asr_app.pdf_preview.open_pdf_in_chrome', return_value={'opened': True, 'browser': 'chrome'}) as opened:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://test') as client:
                    response = await client.post(f'/api/notes/{note["id"]}/open-pdf')
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(opened.call_args.args, (Path(note['path']),))
                    missing = await client.post('/api/notes/missing/open-pdf')
                    self.assertEqual(missing.status_code, 404)
