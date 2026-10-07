from tests import isolation

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qwen_asr_app.course_dates import apply_course_dates, platform_time
from qwen_asr_app.storage import RecordingRecord
from tests.test_phase1 import manager_for, model_stub, request, source


class CourseDatesTests(unittest.TestCase):
    def test_old_manifest_restores_class_times_and_preserves_import_times(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            metadata = {'id': 'old-record', 'status': 'completed', 'started_at': '2026-10-07T12:00:00+08:00',
                        'ended_at': '2026-10-07T12:30:00+08:00', 'source_filename': 'smartclass_old.flac'}
            (directory / 'session.json').write_text(json.dumps(metadata))
            (directory / '课程来源.json').write_text(json.dumps({'title':'课程 · 2026-09-10 13:45–15:20',
                'sources':[{'source_start':'2026-09-10 13:45:00'}, {'source_start':'2026-09-10 14:35:00'}]}))
            record = RecordingRecord.load(directory)
            self.assertEqual(record.public()['started_at'], '2026-09-10T13:45:00+08:00')
            self.assertEqual(record.metadata['ended_at'], '2026-09-10T15:20:00+08:00')
            self.assertEqual(record.metadata['imported_at'], metadata['started_at'])
            self.assertEqual(record.metadata['archived_at'], metadata['ended_at'])
            record.save_metadata()
            self.assertEqual(RecordingRecord.load(directory).metadata, record.metadata)
            self.assertFalse(apply_course_dates(record.metadata, directory))

    def test_microphone_and_ordinary_upload_dates_are_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            for kind in ('microphone', 'upload'):
                metadata = {'kind':kind, 'started_at':'2026-10-07T12:00:00+08:00'}
                original = metadata.copy()
                self.assertFalse(apply_course_dates(metadata, Path(raw)))
                self.assertEqual(metadata, original)

    def test_supplement_uses_earliest_start_and_latest_platform_end(self):
        with tempfile.TemporaryDirectory() as raw:
            metadata = {'status':'completed', 'started_at':'2026-09-10T13:45:00+08:00',
                'imported_at':'2026-10-07T12:00:00+08:00',
                'course_time':{'start':'2026-09-10 13:45:00','end':'2026-09-10 14:30:00'},
                'course_stream':{'sources':[{'start':'2026-09-10 13:45:00','end':'2026-09-10 14:30:00'},
                                            {'start':'2026-09-10 14:35:00','end':'2026-09-10 15:20:00'}]}}
            apply_course_dates(metadata,Path(raw))
            self.assertEqual(metadata['ended_at'],'2026-09-10T15:20:00+08:00')
            self.assertEqual(metadata['started_at'],'2026-09-10T13:45:00+08:00')
            self.assertEqual(metadata['imported_at'],'2026-10-07T12:00:00+08:00')

    def test_legacy_upload_creation_uses_provided_platform_time(self):
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.storage.convert_audio_to_aac'):
            record = RecordingRecord.create_upload(Path(raw)/'unused.flac', 'smartclass_test.flac', 20,
                '课程', '测试', Path(raw), course_time={'start':'2026-09-01 08:00:00', 'end':'2026-09-01 09:35:00'})
            self.assertEqual(record.metadata['started_at'],'2026-09-01T08:00:00+08:00')
            self.assertNotEqual(record.metadata['imported_at'],record.metadata['started_at'])

    def test_platform_timezone_is_explicit_and_invalid_dates_are_ignored(self):
        self.assertEqual(platform_time('2026/09/10 13:45:00'),'2026-09-10T13:45:00+08:00')
        self.assertEqual(platform_time('2026-09-10T05:45:00+00:00'),'2026-09-10T13:45:00+08:00')
        self.assertIsNone(platform_time('invalid'))


class CourseStreamDatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_stream_uses_class_date_from_first_saved_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            manager = manager_for(raw, model_stub())
            data = request('A', [{**source('A'), 'start':'2026-09-10 13:45:00','end':'2026-09-10 14:30:00'}])
            try:
                state = await manager.create(data)
                record = manager.store.find(state['id'])
                self.assertEqual(record.metadata['started_at'],'2026-09-10T13:45:00+08:00')
                self.assertNotEqual(record.metadata['imported_at'],record.metadata['started_at'])
            finally:
                await manager.close()
