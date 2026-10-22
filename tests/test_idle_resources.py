from tests import isolation  # noqa: F401

import asyncio
import copy
import ctypes
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from qwen_asr_app.classroom_intelligence.service import ClassroomIntelligenceService
from qwen_asr_app.durable import atomic_json
from qwen_asr_app.library_query import listing
from qwen_asr_app.recording_catalog import RecordingCatalog
from qwen_asr_app.storage import RecordingRecord, RecordingStore
from qwen_asr_app import runtime_memory


def fixture(root, count=8):
    paths = []
    for index in range(count):
        ident = f'idle-record-{index:04d}'
        folder = root / ident
        folder.mkdir()
        atomic_json(folder / 'session.json', {
            'id': ident, 'kind': 'upload', 'status': 'completed', 'title': ident,
            'category': 'course-a' if index % 2 else 'course-b',
            'sample_count': 160000, 'started_at': '2026-10-01T08:00:00+08:00',
            'ai_note_summary': {'summary': 'private long note\n' * 1000, 'abstract': 'Saved abstract'}})
        atomic_json(folder / 'transcript.json', [{'id': 1, 'raw': 'private transcript ' * 1000,
                                                'start': 0, 'end': 10}])
        atomic_json(folder / 'classroom-notes.json', {'version': 1, 'recording_id': ident, 'notes': []})
        paths.append(folder / 'session.json')
    store = RecordingStore.__new__(RecordingStore)
    store.active = None
    store.paths = {}
    store._read_cache = {}
    store._metadata_paths = lambda: list(paths)
    return store, paths


class CatalogTests(unittest.TestCase):
    def test_unchanged_polls_reuse_small_dtos_and_replacements_invalidate(self):
        with tempfile.TemporaryDirectory() as raw:
            store, paths = fixture(Path(raw))
            with patch.object(RecordingRecord, 'load', wraps=RecordingRecord.load) as load:
                first = list(store.iter_list_items())
                self.assertEqual(load.call_count, 8)
                again = list(store.iter_list_items())
                self.assertEqual(first, again)
                self.assertEqual(load.call_count, 8)
                first[0]['title'] = 'caller mutation'
                self.assertNotEqual(list(store.iter_list_items())[0]['title'], 'caller mutation')
                atomic_json(paths[0].parent / 'transcript.json', [])
                changed = list(store.iter_list_items())
                self.assertEqual(load.call_count, 9)
                self.assertFalse(changed[0]['has_transcript'])
                removed = paths.pop()
                self.assertEqual(len(list(store.iter_list_items())), 7)
                self.assertNotIn(removed, store._catalog().entries)
                self.assertNotIn(removed.parent.name, store.paths)
            serialized = json.dumps(list(store._catalog().entries.values()))
            self.assertNotIn('private long note', serialized)
            self.assertNotIn('private transcript', serialized)
            page = listing(again, category='course-a', limit=2)
            self.assertEqual(page['total'], 4)
            self.assertEqual(page['course_counts']['course-b']['notes'], 4)
            self.assertEqual(page['next_offset'], 2)

    def test_snapshot_budget_does_not_expand_with_library_size(self):
        with tempfile.TemporaryDirectory() as raw:
            store, _ = fixture(Path(raw), 12)
            catalog = RecordingCatalog(store, max_bytes=2000)
            self.assertEqual(len(list(catalog.items())), 12)
            self.assertLessEqual(catalog.bytes, 2000)
            self.assertLess(len(catalog.entries), 12)

    def test_open_record_uses_unsaved_live_counters(self):
        with tempfile.TemporaryDirectory() as raw:
            store, paths = fixture(Path(raw), 1)
            list(store.iter_list_items())
            store.active = RecordingRecord.load(paths[0].parent)
            store.active.metadata['sample_count'] = 320000
            self.assertEqual(list(store.iter_list_items())[0]['duration'], 20)


class IdleBackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_discard_receipts_do_not_reload_missing_recordings_and_restored_rows_are_current(self):
        import app
        from qwen_asr_app.recording_discard import RecordingDiscard
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, paths = fixture(root, 2)
            discard = RecordingDiscard(store, {}, AsyncMock(), root / 'receipts.json')
            discard.results['deleted-record-123'] = {'status': 'discarded'}
            with patch.object(app, 'recording_store', store), patch.object(app, 'recording_discard', discard), \
                 patch.object(app, 'discover_courses'):
                await app.recording_library(include_settings=False)
                with patch.object(RecordingRecord, 'load', side_effect=AssertionError('missing discard triggered full scan')):
                    await app.recording_library(include_settings=False)
                    await app.recording_library(include_settings=False)
                metadata = json.loads(paths[0].read_text())
                metadata.update(kind='microphone', discard_job={'status': 'discarded'})
                atomic_json(paths[0], metadata)
                atomic_json(paths[0].parent / '.recording-discard.json', {
                    'session_id': metadata['id'], 'original_path': str(paths[0].parent)})
                discard.results[metadata['id']] = {
                    'status': 'discarded', 'trashed_path': str(root / 'no-longer-in-trash')}
                response = await app.recording_library(include_settings=False)
                self.assertEqual(discard.results[metadata['id']]['status'], 'restored')
                restored = next(row for row in response['records'] if row['id'] == metadata['id'])
                self.assertEqual(restored, store.find(metadata['id']).list_item())
                self.assertEqual(json.loads(paths[0].read_text())['discard_job']['status'], 'restored')

    async def test_empty_retry_scan_never_decodes_recordings_and_only_changed_notes_are_read(self):
        with tempfile.TemporaryDirectory() as raw:
            store, paths = fixture(Path(raw), 20)
            service = ClassroomIntelligenceService(store)
            service.enqueue_enhancement = AsyncMock()
            with patch.object(RecordingRecord, 'load', side_effect=AssertionError('idle transcript load')):
                await service._retry_once()
                with patch.object(Path, 'read_text', side_effect=AssertionError('unchanged JSON reread')):
                    await service._retry_once()
            note = {'id': 'due-note', 'status': 'failed', 'job': {'status': 'failed', 'retryable': True, 'next_retry_at': 0}}
            atomic_json(paths[0].parent / 'classroom-notes.json',
                        {'recording_id': paths[0].parent.name, 'notes': [note]})
            with patch.object(RecordingRecord, 'load', wraps=RecordingRecord.load) as load:
                await service._retry_once()
                self.assertEqual(load.call_count, 1)
                service.enqueue_enhancement.assert_awaited_once()
                self.assertEqual(service.enqueue_enhancement.await_args.args[1], 'due-note')
            note['locked'] = True
            atomic_json(paths[0].parent / 'classroom-notes.json',
                        {'recording_id': paths[0].parent.name, 'notes': [note]})
            service.enqueue_enhancement.reset_mock()
            await service._retry_once()
            service.enqueue_enhancement.assert_not_awaited()

    async def test_idle_task_cache_invalidates_on_saved_edits_and_preserves_fresh_live_activity(self):
        import app
        with tempfile.TemporaryDirectory() as raw:
            store, paths = fixture(Path(raw), 2)
            app._idle_ai_task_cache.clear()
            with patch.object(app, 'recording_store', store), patch.object(app, 'live_sessions', {}), \
                 patch.object(app, 'record_operations', {}), patch.object(app, 'running_batch_ids', set()), \
                 patch.object(app, 'background_ai_tasks', {}), patch.object(app, 'background_summary_tasks', {}), \
                 patch.object(app, 'finalization_tasks', {}), \
                 patch.object(app, '_build_compact_ai_tasks', wraps=app._build_compact_ai_tasks) as build:
                initial = app.compact_ai_tasks()
                self.assertTrue(initial)
                self.assertEqual(app.compact_ai_tasks(), initial)
                self.assertEqual(build.call_count, 1)
                initial[0]['recording_title'] = 'caller mutation'
                self.assertNotEqual(app.compact_ai_tasks()[0]['recording_title'], 'caller mutation')
                data = json.loads(paths[0].read_text())
                data['title'] = 'Updated title'
                atomic_json(paths[0], data)
                updated = app.compact_ai_tasks()
                self.assertTrue(any(row['recording_title'] == 'Updated title' for row in updated))
                self.assertEqual(build.call_count, 2)
                running = asyncio.create_task(asyncio.Event().wait())
                try:
                    app.background_ai_tasks['running'] = running
                    app.compact_ai_tasks()
                    app.compact_ai_tasks()
                    self.assertEqual(build.call_count, 4)
                finally:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    app._idle_ai_task_cache.clear()


class MemoryObservationTests(unittest.TestCase):
    def test_darwin_current_rss_uses_native_task_info_without_a_subprocess(self):
        def inspect(pid, flavor, arg, pointer, size):
            self.assertEqual(flavor, 4)
            self.assertEqual(size, 96)
            ctypes.cast(pointer, ctypes.POINTER(runtime_memory._ProcTaskInfo)).contents.resident_size = 123456789
            return size
        with patch.object(runtime_memory, '_darwin_proc_info', return_value=inspect):
            self.assertEqual(runtime_memory._darwin_rss_bytes(), 123456789)
        with patch.object(runtime_memory, '_darwin_proc_info', return_value=lambda *args: 0):
            self.assertIsNone(runtime_memory._darwin_rss_bytes())

    @unittest.skipUnless(__import__('sys').platform == 'darwin', 'native macOS API')
    def test_native_macos_current_rss(self):
        self.assertGreater(runtime_memory.rss_bytes(), 0)


if __name__ == '__main__':
    unittest.main()
