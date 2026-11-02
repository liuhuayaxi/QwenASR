from tests import isolation  # Must precede application imports.

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qwen_asr_app import storage
from qwen_asr_app.storage import RecordingRecord, RecordingStore


class ReadOnlyStoreTests(unittest.TestCase):
    def test_list_keeps_directory_snapshot_when_another_reader_changes_paths(self):
        store = RecordingStore.__new__(RecordingStore)
        store.paths = {'first-record': Path('/first'), 'second-record': Path('/second')}
        store.active = None
        first = SimpleNamespace(id='first-record', metadata={'started_at': '1'})
        second = SimpleNamespace(id='second-record', metadata={'started_at': '2'})
        class Cache(dict):
            def get(self, path):
                if path == Path('/first/session.json'):
                    store.paths.pop('second-record', None)
                return super().get(path)
        store._read_cache = Cache({Path('/first/session.json'): (None, first),
                                   Path('/second/session.json'): (None, second)})
        with patch.object(store, 'refresh'):
            result = store.list_records()
        self.assertEqual([record.id for record in result], ['second-record', 'first-record'])

    def test_refresh_prunes_a_snapshot_when_cache_changes_during_pruning(self):
        store = RecordingStore.__new__(RecordingStore)
        store.paths = {}
        store.active = None
        record = SimpleNamespace(metadata={})
        class CacheKey:
            @property
            def parent(self):
                store._read_cache[Path('/new/session.json')] = ((), record)
                return Path('/removed')
        store._read_cache = {CacheKey(): ((), record)}
        with patch.object(store, '_metadata_paths', return_value=[]):
            store.refresh()
        self.assertEqual(store._read_cache, {})

    def fixture(self, root):
        record = RecordingRecord(root / '目录类别' / '课堂', {
            'id': 'read-only-fixture', 'title': '课堂', 'category': '旧类别',
            'kind': 'upload', 'status': 'processing', 'sample_count': 0,
            'summary_job': {'status': 'running'},
            'finalization': {'status': 'running'},
            'ai_jobs': {'sentence': {'status': 'running'}},
            'ai_note_summary_draft': {'parts': {'0': '保留的成功内容'}},
        })
        record.save_all()
        return record


    def test_safe_component_rejects_windows_path_separators(self):
        value = storage.safe_component(r"..\\secret", "fallback")
        self.assertNotIn("\\", value)
        self.assertNotIn("/", value)
        self.assertNotEqual(value, "..")

    def test_constructor_and_list_queries_do_not_rewrite_running_records(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = self.fixture(root)
            before = {p.name: p.read_bytes() for p in record.directory.iterdir() if p.is_file()}
            with patch.object(RecordingStore, '_metadata_paths', return_value=[record.meta_path]), \
                    patch.object(storage, 'shared', return_value=True), \
                    patch.object(storage.app_settings, 'storage_root', root):
                store = RecordingStore()
                self.assertEqual(store.find(record.id).metadata['status'], 'processing')
                store.list_records()
                store.refresh()
            after = {p.name: p.read_bytes() for p in record.directory.iterdir() if p.is_file()}
            self.assertEqual(before, after)

    def test_only_service_startup_reconciles_interruption_under_exclusive_ownership(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = self.fixture(root)
            with patch.object(RecordingStore, '_metadata_paths', return_value=[record.meta_path]), \
                    patch.object(storage, 'PRIVATE_ROOT', root / 'private'), \
                    patch.object(storage, 'shared', return_value=True), \
                    patch.object(storage.app_settings, 'storage_root', root):
                first, second = RecordingStore(), RecordingStore()
                with first.service_startup():
                    saved = first.find(record.id)
                    self.assertEqual(saved.metadata['status'], 'interrupted')
                    self.assertEqual(saved.metadata['category'], '目录类别')
                    self.assertEqual(saved.metadata['summary_job']['status'], 'interrupted')
                    self.assertEqual(saved.metadata['ai_jobs']['sentence']['status'], 'interrupted')
                    self.assertEqual(saved.metadata['ai_note_summary_draft']['parts']['0'], '保留的成功内容')
                    saved.metadata['status'] = 'processing'
                    saved.save_metadata()
                    before = saved.meta_path.read_bytes()
                    with self.assertRaisesRegex(RuntimeError, '拒绝重复启动'):
                        with second.service_startup():
                            self.fail('second service must not enter')
                    self.assertEqual(saved.meta_path.read_bytes(), before)
                # A later owner can recover only after the previous one exits.
                with second.service_startup():
                    self.assertEqual(second.find(record.id).metadata['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
