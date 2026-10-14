from tests import isolation  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from qwen_asr_app.note_library import NoteLibrary


class UnifiedReviewCatalogTests(unittest.TestCase):
    def make_library(self, root):
        settings = SimpleNamespace(storage_root=root, categories=['未分类', '旧课程'])
        recordings = SimpleNamespace(list_records=lambda: [])
        with patch('qwen_asr_app.note_library.NOTE_STATE_PATH', root / 'catalog.json'):
            library = NoteLibrary(settings, recordings)
        for category, title in [('旧课程', '复习笔记'), ('未分类', '其他笔记')]:
            temporary = root / (title + '.pdf')
            temporary.write_bytes(b'%PDF-synthetic')
            library.add(category, title, temporary.name, [], temporary)
        return library

    def test_reclassifying_course_updates_relative_paths_and_preserves_other_courses(self):
        with tempfile.TemporaryDirectory() as raw:
            library = self.make_library(Path(raw))
            original = library.notes('旧课程')[0]
            library.reclassify('旧课程', '新课程')
            (library.storage_root / '旧课程').rename(library.storage_root / '新课程')
            migrated = library.notes('新课程')[0]
            self.assertEqual(migrated['id'], original['id'])
            self.assertNotEqual(migrated['path'], original['path'])
            self.assertEqual(Path(migrated['path']).parent, library.storage_root / '新课程' / 'AI生成笔记')
            self.assertTrue(Path(migrated['path']).is_file())
            self.assertEqual(len(library.notes('未分类')), 1)

    def test_failed_course_delete_restores_only_original_catalog_labels(self):
        with tempfile.TemporaryDirectory() as raw:
            library = self.make_library(Path(raw))
            before = [(item['id'], item['category']) for item in library.notes()]
            with patch.object(app, 'review_library', library), \
                 patch.object(app.recording_store, 'list_records', return_value=[]), \
                 patch.object(app, 'change_category', side_effect=ValueError('目录冲突')):
                with self.assertRaises(app.HTTPException):
                    app.change_library_category('旧课程')
            self.assertEqual([(item['id'], item['category']) for item in library.notes()], before)
            saved = json.loads(library.state_path.read_text())
            self.assertEqual({item['id']: item['category'] for item in saved['notes']}, dict(before))
