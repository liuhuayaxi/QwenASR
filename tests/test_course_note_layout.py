from tests import isolation  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qwen_asr_app.note_library import NoteLibrary
from qwen_asr_app.storage import AppSettings
from qwen_asr_app.categories import change_category
from qwen_asr_app.shared_files import SharedCourseFiles


class CourseNoteLayoutTests(unittest.TestCase):
    def settings(self, root, layout='shared-courses-v1'):
        settings = AppSettings.__new__(AppSettings)
        root = root.resolve()
        settings.storage_root = root
        settings.categories = ['未分类', '物理光学']
        settings.known_roots = [str(root)]
        settings.category_profiles = {}
        settings.library_layout = layout
        settings.save = lambda: None
        root.mkdir(exist_ok=True)
        return settings

    def library(self, settings, state):
        recordings = SimpleNamespace(list_records=lambda: [], refresh=lambda: None)
        with patch('qwen_asr_app.note_library.NOTE_STATE_PATH', state):
            return NoteLibrary(settings, recordings)

    def test_migrates_catalog_and_uncataloged_files_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.settings(root / '资料')
            legacy = root / '独立笔记'
            (legacy / '物理光学/附录').mkdir(parents=True)
            old = legacy / '物理光学/复习.pdf'
            old.write_bytes(b'%PDF-original')
            (legacy / '物理光学/附录/公式.txt').write_bytes(b'formula')
            (legacy / '.qwenasr-note-library.json').write_text('{}')
            state = root / 'catalog.json'
            state.write_text(json.dumps({'storage_root':str(legacy), 'notes':[{'id':'stable-id', 'category':'物理光学','path':str(old)}]}))
            library = self.library(settings, state)
            target = settings.storage_root / '物理光学/AI生成笔记/复习.pdf'
            self.assertEqual(target.read_bytes(), b'%PDF-original')
            self.assertEqual((target.parent / '附录/公式.txt').read_bytes(), b'formula')
            self.assertEqual(library.notes()[0]['path'], str(target))
            self.assertEqual(library.notes()[0]['id'], 'stable-id')
            self.assertFalse(legacy.exists())
            self.assertEqual(json.loads(state.read_text())['notes'][0]['path'], '物理光学/AI生成笔记/复习.pdf')
            self.assertEqual(self.library(settings, state).notes()[0]['path'], str(target))
            self.assertFalse((settings.storage_root / 'AI生成笔记').exists())

    def test_collisions_and_save_failure_preserve_originals_and_catalog(self):
        for failure in ('collision', 'save'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                settings = self.settings(root / '资料')
                legacy = settings.storage_root / 'AI生成笔记'
                old = legacy / '物理光学/notes.pdf'
                old.parent.mkdir(parents=True)
                old.write_bytes(b'original')
                state = root / 'catalog.json'
                state.write_text(json.dumps({'storage_root':str(legacy), 'notes':[]}))
                before = state.read_bytes()
                if failure == 'collision':
                    target = settings.storage_root / '物理光学/AI生成笔记/notes.pdf'
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'keep target')
                    with self.assertRaises(ValueError):
                        self.library(settings, state)
                    self.assertEqual(target.read_bytes(), b'keep target')
                else:
                    with patch('qwen_asr_app.note_library.atomic_json', side_effect=OSError('disk')):
                        with self.assertRaises(OSError):
                            self.library(settings, state)
                self.assertEqual(old.read_bytes(), b'original')
                self.assertEqual(state.read_bytes(), before)

    def test_upload_whole_library_move_and_category_merge_keep_notes_available(self):
        for layout in ('legacy', 'shared-courses-v1'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                settings = self.settings(root / '资料', layout)
                library = self.library(settings, root / 'catalog.json')
                temporary = root / 'upload.pdf'
                temporary.write_bytes(b'%PDF-upload')
                note = library.add('物理光学', '期末复习', 'upload.pdf', [], temporary)
                self.assertEqual(Path(note['path']).parent, settings.storage_root / '物理光学/AI生成笔记')
                self.assertEqual(SharedCourseFiles(settings).scan('物理光学'), [])
                target = root / '新资料'
                settings.migrate_storage_root(str(target))
                self.assertEqual(Path(library.notes()[0]['path']).read_bytes(), b'%PDF-upload')
                library.reclassify('物理光学', '未分类')
                change_category(settings, library.recordings, '物理光学')
                self.assertEqual(Path(library.notes('未分类')[0]['path']).read_bytes(), b'%PDF-upload')

    def test_corrupt_catalog_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.settings(root / '资料')
            state = root / 'catalog.json'
            state.write_text('{broken')
            with self.assertRaises(ValueError):
                self.library(settings, state)
            self.assertEqual(state.read_text(), '{broken')

    def test_custom_legacy_root_keeps_unrelated_documents_and_empty_folders(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.settings(root / '资料')
            legacy = root / 'Documents'
            (legacy / '物理光学').mkdir(parents=True)
            (legacy / '物理光学/复习.pdf').write_bytes(b'%PDF-note')
            (legacy / '个人文件/空目录').mkdir(parents=True)
            (legacy / '个人文件/private.txt').write_text('keep')
            (legacy / '不要动.txt').write_text('keep top level')
            state = root / 'catalog.json'
            state.write_text(json.dumps({'storage_root':str(legacy),'notes':[]}))
            self.library(settings, state)
            self.assertEqual((settings.storage_root / '物理光学/AI生成笔记/复习.pdf').read_bytes(), b'%PDF-note')
            self.assertEqual((legacy / '个人文件/private.txt').read_text(), 'keep')
            self.assertEqual((legacy / '不要动.txt').read_text(), 'keep top level')
            self.assertTrue((legacy / '个人文件/空目录').is_dir())
