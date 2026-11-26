from tests import isolation
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from qwen_asr_app.ideashell_auto_push import AutoPushQueue


class AutoPushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.record = SimpleNamespace(id='class-1', metadata={
            'status': 'completed', 'ai_note_summary': {'summary': '笔记正文'}}, save_metadata=lambda: None)
        self.deliver = AsyncMock(return_value={'synced': True})
        self.queue = AutoPushQueue(Path(self.temp.name) / 'queue.json', lambda _: self.record,
                                   lambda _: False, self.deliver)

    async def test_every_regeneration_delivers_even_identical_text(self):
        self.queue.enqueue(self.record, 'first')
        self.queue.enqueue(self.record, 'first')
        self.queue.enqueue(self.record, 'second')
        self.assertEqual(len(self.queue.items), 2)
        for item in self.queue.items:
            await self.queue.process(item)
        self.assertEqual(self.deliver.await_count, 2)
        self.assertEqual(self.record.metadata['ideashell_auto_push']['status'], 'completed')

    async def test_busy_record_waits_without_consuming_retry(self):
        self.queue.enqueue(self.record)
        self.queue.busy = lambda _: True
        await self.queue.process(self.queue.items[0])
        self.deliver.assert_not_awaited()
        self.assertEqual(self.queue.items[0]['attempts'], 0)

    async def test_durable_restart_recovers_pending_delivery(self):
        self.queue.enqueue(self.record, 'first')
        self.queue.items[0]['status'] = 'running'
        self.queue.save()
        recovered = AutoPushQueue(self.queue.path, lambda _: self.record, lambda _: False, self.deliver)
        await recovered.process(recovered.items[0])
        self.assertEqual(recovered.items[0]['status'], 'completed')

    async def test_unknown_create_never_creates_another_note(self):
        self.record.metadata['ideashell_sync'] = {'create_operation': {'status': 'unknown'}}
        self.queue.enqueue(self.record)
        await self.queue.process(self.queue.items[0])
        self.deliver.assert_not_awaited()
        self.assertEqual(self.queue.items[0]['status'], 'failed')

    async def test_transient_error_is_retryable_without_losing_note(self):
        self.deliver.side_effect = RuntimeError('暂时不可达')
        self.queue.enqueue(self.record)
        item = self.queue.items[0]
        await self.queue.process(item)
        self.assertEqual(item['status'], 'waiting_retry')
        self.assertEqual(self.record.metadata['ai_note_summary']['summary'], '笔记正文')
        self.deliver.side_effect = None
        await self.queue.process(item)
        self.assertEqual(item['status'], 'completed')

    async def test_no_note_is_not_enqueued(self):
        self.record.metadata.pop('ai_note_summary')
        self.queue.enqueue(self.record)
        self.assertEqual(self.queue.items, [])

    async def test_automatic_payload_uses_combined_text_without_audio(self):
        import app
        self.record.metadata['ideashell_sync'] = {'server_url': 'https://mcp.ideashell.cn/mcp'}
        preview = AsyncMock(return_value={'digest': 'text', 'assets_digest': 'images'})
        push = AsyncMock(return_value={'synced': True})
        with patch.object(app.recording_store, 'find', return_value=self.record), \
             patch.object(app, 'preview_ideashell_push', preview), \
             patch.object(app, 'push_recording_summary_to_ideashell', push):
            await app.auto_push_generated_note(self.record.id)
        payload = push.await_args.args[1]
        self.assertEqual(payload['content_mode'], 'combined')
        self.assertFalse(payload['include_audio'])
        self.assertEqual(payload['assets_digest'], 'images')
        self.assertNotIn('create_new', payload)

    async def test_completed_remote_receipt_prevents_duplicate_after_restart(self):
        import app
        self.record.metadata['ideashell_sync'] = {'status': 'synced', 'auto_generation_id': 'first'}
        push = AsyncMock()
        with patch.object(app.recording_store, 'find', return_value=self.record), \
             patch.object(app, 'push_recording_summary_to_ideashell', push):
            self.assertTrue((await app.auto_push_generated_note(self.record.id, 'first'))['synced'])
        push.assert_not_awaited()

    async def test_generation_endpoint_enqueues_first_and_regenerated_notes(self):
        import app
        from qwen_asr_app.storage import RecordingRecord
        record = RecordingRecord(Path(self.temp.name), {
            'id': 'generated-class', 'status': 'completed', 'title': '考试复习', 'category': '合成课程',
            'ai_note_summary': {'summary': '已有笔记'},
            'ideashell_sync': {'status': 'synced', 'note_id': 'a' * 32}})
        record.segments = [{'id': 1, 'raw': '录音原文', 'start': 0, 'end': 1}]
        record.save_all()

        async def build(saved, config):
            saved.metadata['ai_note_summary'] = {'summary': '本次新笔记'}
            saved.save_all()
            return saved.metadata['ai_note_summary']

        enqueue = Mock()
        with patch.object(app.recording_store, 'find', return_value=record), \
             patch.object(app.ai_preferences, 'task_config', return_value={}), \
             patch.object(app, 'build_note_summary', side_effect=build), \
             patch.object(app, 'generate_recording_title', AsyncMock(return_value='考试复习')), \
             patch.object(app, 'apply_recording_title', return_value=record), \
             patch.object(app.ideashell_auto_push, 'enqueue', enqueue):
            for generation in ('first', 'regenerated'):
                await app.generate_library_summary(record.id, {'_note_generation_id': generation})
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual([c.args[1] for c in enqueue.call_args_list], ['first', 'regenerated'])

    async def test_saved_body_is_queued_even_if_later_review_fails(self):
        import app
        from fastapi import HTTPException
        from qwen_asr_app.storage import RecordingRecord
        record=RecordingRecord(Path(self.temp.name),{'id':'saved-before-failure','status':'completed',
            'title':'合成课堂','category':'合成课程','ai_note_summary':{'summary':'前一版'}})
        record.segments=[{'id':1,'raw':'原文','start':0,'end':1}]
        record.save_all()
        async def fail_after_save(saved,config):
            saved.metadata['ai_note_summary']={'summary':'新正文已经持久化'}
            saved.save_all()
            raise RuntimeError('synthetic review failure after durable note save')
        enqueue=Mock()
        with patch.object(app.recording_store,'find',return_value=record), \
             patch.object(app.ai_preferences,'task_config',return_value={}), \
             patch.object(app,'build_note_summary',side_effect=fail_after_save), \
             patch.object(app.ideashell_auto_push,'enqueue',enqueue):
            with self.assertRaises(HTTPException):
                await app.generate_library_summary(record.id,{'_note_generation_id':'new-body'})
        enqueue.assert_called_once_with(record,'new-body')


if __name__ == '__main__':
    unittest.main()
