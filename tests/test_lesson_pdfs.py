from tests import isolation  # noqa: F401

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app
from qwen_asr_app.lesson_pdfs import lesson_pdfs
from qwen_asr_app.drive_backup import inventory, DEFAULTS
from qwen_asr_app.note_library import NoteLibrary
from qwen_asr_app.storage import RecordingRecord


class LessonPdfTests(unittest.IsolatedAsyncioTestCase):
    def setup_library(self, root):
        lesson = root / 'library' / '课程' / '上课录音' / 'lesson'
        lesson.mkdir(parents=True)
        record = RecordingRecord(lesson, {'id': 'lesson', 'title': '本节课', 'category': '课程'})
        settings = SimpleNamespace(storage_root=root / 'library', categories=['课程'])
        recordings = SimpleNamespace(list_records=lambda: [record])
        self.record = record
        with patch('qwen_asr_app.note_library.NOTE_STATE_PATH', root / 'catalog.json'):
            return NoteLibrary(settings, recordings)

    async def test_linked_pdf_lives_in_lesson_and_downloads_without_material_catalog_entry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = self.setup_library(root)
            source = root / 'export.pdf'
            data = b'%PDF-preserved'
            source.write_bytes(data)
            result = library.add('课程', '本节课笔记', source.name, ['lesson'], source)
            self.assertEqual(library.notes(), [])
            self.assertTrue(Path(result['path']).is_relative_to(self.record.directory.resolve() / '.note-pdfs'))
            self.assertEqual(list((root / 'library' / '课程' / 'AI生成笔记').glob('*.pdf')), [])
            self.assertEqual(self.record.public()['note_pdfs'][0]['sha256'], hashlib.sha256(data).hexdigest())
            plan = inventory(library.settings, library.recordings, library, DEFAULTS)
            self.assertEqual([item['path'] for item in plan], [Path(result['path'])])
            with patch.object(app.recording_store, 'find', return_value=self.record):
                response = await app.download_saved_lesson_pdf('lesson', result['id'])
            self.assertEqual(Path(response.path).read_bytes(), data)

    def test_existing_versions_move_with_byte_preservation_and_idempotent_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = self.setup_library(root)
            old = library.folder('课程') / 'original.pdf'
            old.write_bytes(b'%PDF-old-version')
            entry = {'id': 'a' * 32, 'category': '课程', 'title': '旧笔记', 'filename': old.name,
                'path': old.relative_to(library.storage_root).as_posix(), 'source_session_ids': ['lesson']}
            library.state['notes'] = [entry]
            library._save()
            library._archive_lesson_pdfs()
            self.assertFalse(old.exists())
            self.assertEqual(library.notes(), [])
            self.assertEqual(lesson_pdfs(self.record)[0]['sha256'], hashlib.sha256(b'%PDF-old-version').hexdigest())
            library._archive_lesson_pdfs()
            self.assertEqual(len(lesson_pdfs(self.record)), 1)

    def test_catalog_write_failure_restores_source_and_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = self.setup_library(root)
            old = library.folder('课程') / 'original.pdf'
            old.write_bytes(b'%PDF-old-version')
            entry = {'id': 'a' * 32, 'category': '课程', 'title': '旧笔记', 'filename': old.name,
                'path': old.relative_to(library.storage_root).as_posix(), 'source_session_ids': ['lesson']}
            library.state['notes'] = [entry]
            library._save()
            before = library.state_path.read_bytes()
            with patch.object(library, '_save', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    library._archive_lesson_pdfs()
            self.assertEqual(old.read_bytes(), b'%PDF-old-version')
            self.assertEqual(lesson_pdfs(self.record), [])
            self.assertEqual(library.state_path.read_bytes(), before)
            self.assertEqual(library.state['notes'], [entry])
