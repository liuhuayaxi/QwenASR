from tests import isolation

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qwen_asr_app.course_import_status import lesson_import_statuses, recording_inventory
from qwen_asr_app.smartclass import course_key, lesson_groups
from tests import test_smartclass

COURSE = test_smartclass.GroupTests.course
video = test_smartclass.video


class ImportStatusTests(unittest.TestCase):
    def test_completed_lesson_preserves_vga_fallback_warning(self):
        warning = 'VGA 提取不可用，已回退到本地资料库课件'
        history = {self.group['id']: {'record_id': 'record-0001', 'state': 'completed', 'warnings': [warning]}}
        result = self.status([self.record()], history)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['warnings'], [warning])
        self.assertIn(warning, result['detail'])
        history[self.group['id']]['vga'] = {'status': 'completed', 'asset_id': 'courseware'}
        result = self.status([self.record()], history)
        self.assertTrue(result['vga_ready'])
        self.assertNotIn(warning, result['detail'])
        self.assertIn('VGA 课件 PDF 已归档', result['detail'])

    def setUp(self):
        self.videos = [video('a', '2026-09-03 13:45:00', '2026-09-03 14:30:00', 6),
                       video('b', '2026-09-03 14:35:00', '2026-09-03 15:20:00', 7)]
        self.group = lesson_groups(self.videos, COURSE)[0]

    def record(self, ids=('a', 'b'), status='completed'):
        return {'id': 'record-0001', 'title': '人工修改过的标题', 'status': status,
                'sources': [{'video_id': ident, 'segment_id': 1} for ident in ids]}

    def status(self, records=(), imports=None, queue=(), group=None):
        group = group or self.group
        return lesson_import_statuses([group], records, imports or {}, queue)[group['id']]

    def test_existing_renamed_archive_matches_both_periods_without_import_history(self):
        result = self.status([self.record()])
        self.assertEqual(result['label'], '已入库')
        self.assertEqual(result['completed'], 2)
        self.assertTrue(all(p['state'] == 'completed' for p in result['periods']))

    def test_same_title_without_source_identity_does_not_match(self):
        record = {**self.record(()), 'title': self.group['title']}
        self.assertEqual(self.status([record])['state'], 'new')

    def test_single_period_then_pair_and_pair_then_single(self):
        result = self.status([self.record(('a',))])
        self.assertEqual(result['label'], '部分入库')
        self.assertEqual([p['state'] for p in result['periods']], ['completed', 'new'])
        single = lesson_groups(self.videos[:1], COURSE)[0]
        self.assertEqual(self.status([self.record()], group=single)['label'], '已入库')

    def test_legacy_filename_and_source_manifest_are_supported(self):
        record = self.record(())
        record['source_filename'] = 'smartclass_' + self.group['id'] + '.flac'
        self.assertEqual(self.status([record])['state'], 'completed')
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            (path / '课程来源.json').write_text(json.dumps({'sources': self.record()['sources']}))
            item = SimpleNamespace(id='legacy', metadata={'status': 'completed'}, directory=path)
            self.assertEqual(self.status(recording_inventory([item]))['state'], 'completed')

    def test_attempts_remain_visible_after_serialization_without_claiming_completion(self):
        for state, label in [('queued', '已排队'), ('running', '入库中'), ('paused', '已暂停'), ('interrupted', '待继续')]:
            with self.subTest(state=state):
                queue = json.loads(json.dumps([{'group': self.group, 'state': state}]))
                self.assertEqual(self.status(queue=queue)['label'], label)
                self.assertEqual(self.status(queue=queue)['completed'], 0)

    def test_completed_audio_is_archived_even_when_summary_fails(self):
        history = {self.group['id']: {'record_id': 'record-0001', 'state': 'interrupted'}}
        queue = [{'group': self.group, 'state': 'interrupted'}]
        self.assertEqual(self.status([self.record()], history, queue)['label'], '已入库')

    def test_deleted_archive_is_not_claimed_as_currently_imported(self):
        history = {self.group['id']: {'record_id': 'record-0001', 'state': 'completed'}}
        self.assertEqual(self.status(imports=history)['label'], '已移除')

    def test_old_single_period_is_kept_while_supplement_is_in_progress(self):
        single = lesson_groups(self.videos[:1], COURSE)[0]
        history = {single['id']: {'record_id': 'record-0001', 'video_ids': ['a'], 'state': 'completed'}}
        result = self.status([self.record(status='processing')], history,
                             [{'group': self.group, 'state': 'running'}])
        self.assertEqual(result['state'], 'running')
        self.assertEqual([p['state'] for p in result['periods']], ['completed', 'running'])


class StatusRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_lessons_and_polling_use_local_inventory_without_fetching_school_again(self):
        from qwen_asr_app import smartclass_service as service
        course = COURSE
        key = course_key(course)
        videos = [video('a', '2026-09-03 13:45:00', '2026-09-03 14:30:00', 6)]
        with tempfile.TemporaryDirectory() as raw:
            record = SimpleNamespace(id='existing', directory=Path(raw), metadata={
                'status': 'completed', 'course_stream': {'sources': [{'video_id': 'a', 'segment_id': 1}]}})
            store = SimpleNamespace(list_records=lambda: [record])
            with patch.object(service.app.state, 'recording_store', store, create=True), \
                    patch.object(service, 'course_rows', {key: course}), patch.object(service, 'group_rows', {}), \
                    patch.object(service, 'imports', {}), patch.object(service, 'queue_state', {'items': []}), \
                    patch.object(service.client, 'ensure_login', return_value={'name': 'test'}), \
                    patch.object(service.client, 'videos', Mock(return_value=videos)) as read_videos:
                response = await service.lessons(key)
                group = response['groups'][0]
                self.assertEqual(group['import_status']['label'], '已入库')
                record.metadata['status'] = 'interrupted'
                status = await service.get_status(key)
                self.assertEqual(status['lesson_statuses'][group['id']]['label'], '待继续')
                read_videos.assert_called_once()
