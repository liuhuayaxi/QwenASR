from tests import isolation  # noqa: F401

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import app
from qwen_asr_app import storage
from qwen_asr_app.durable import atomic_json
from qwen_asr_app.note_checkpoints import checkpoint_draft
from qwen_asr_app.review_notes import stage
from qwen_asr_app.review_notes import model_json
from qwen_asr_app.storage import RecordingRecord, RecordingStore
from tests.test_live_recovery import store_for


class CheckpointRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_postprocessing_phase_takes_precedence_over_completed_writer_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            record=RecordingRecord(Path(raw),{'id':'postprocessing','summary_job':{'checkpoint':{'review_task':'a'*24}}})
            folder=record.directory/'.review-notes'/('a'*24);folder.mkdir(parents=True)
            atomic_json(folder/'task.json',{'id':'a'*24,'stage':'完成 · 未审核','status':'completed'})
            for phase in ['核对课件跳转','AI 取名']:
                record.metadata['ai_note_summary_draft']={'phase':phase,'parts':{},'total_parts':0}
                self.assertEqual(app._summary_phase_progress(record,{})['phase'],phase)

    async def test_continue_preserves_saved_custom_prompt_without_accepting_browser_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(Path(raw), {
                'id': 'prompt-resume', 'title': '课堂', 'category': '测试课程', 'status': 'completed',
                'summary_job': {'status': 'interrupted', 'custom_prompt': '保留老师完整推导和例题'},
            })
            record.segments = [{'id': 1, 'raw': '完整原文', 'start': 0, 'end': 3}]
            record.save_all()
            with patch.object(app, 'recording_store', store_for(record)), \
                 patch.object(app, '_saved_ai_task_config', return_value={'ai': {'saved': True}}), \
                 patch.object(app, 'start_summary_job') as start:
                result = await app.ai_task_action({'recording_id': record.id, 'type': 'summary',
                    'action': 'continue', 'config': {'untrusted_browser_provider': True}})
            self.assertTrue(result['accepted'])
            payload = start.call_args.args[1]
            self.assertEqual(payload['custom_prompt'], '保留老师完整推导和例题')
            self.assertEqual(payload['config']['ai'], {'saved': True})
            self.assertNotIn('untrusted_browser_provider', payload['config'])

    async def test_completed_model_response_replays_after_exit_without_another_request(self):
        from qwen_asr_app.agy_cli import last_session_id
        config = {'ai': {'summary_provider': {'transport': 'agy_cli',
                  'cli_model': 'gemini-3.8-flash-high', 'context_window': 1048576}}}
        with tempfile.TemporaryDirectory() as raw:
            response_path = Path(raw) / 'coverage-response-0-attempt-0.txt'
            async def answer(**kwargs):
                last_session_id.set('completed-session')
                return '{"knowledge":[]}'
            with patch('qwen_asr_app.review_notes.request_ai_text', side_effect=answer) as request:
                first = await model_json('知识清单', {'transcript': '完整来源'}, config, response_path=response_path)
            request.assert_awaited_once()
            before = response_path.read_bytes()
            with patch('qwen_asr_app.review_notes.request_ai_text', new=AsyncMock()) as request:
                restored = await model_json('知识清单', {'transcript': '完整来源'}, config, response_path=response_path)
                self.assertEqual(restored, first)
                self.assertEqual(restored['_provider_session_id'], 'completed-session')
                request.assert_not_awaited()
                with self.assertRaisesRegex(ValueError, '检查点'):
                    await model_json('知识清单', {'transcript': ' changed input'}, config, response_path=response_path)
                request.assert_not_awaited()
            self.assertEqual(response_path.read_bytes(), before)
            response_path.write_text('{"tampered":true}')
            with patch('qwen_asr_app.review_notes.request_ai_text', new=AsyncMock()) as request:
                with self.assertRaisesRegex(ValueError, '检查点'):
                    await model_json('知识清单', {'transcript': '完整来源'}, config, response_path=response_path)
                request.assert_not_awaited()

    def fixture(self, root):
        record = RecordingRecord(root / 'lesson', {
            'id': 'checkpoint-lesson', 'title': '保留课堂', 'category': '未分类',
            'kind': 'upload', 'status': 'completed', 'summary_job': {'status': 'running'},
        })
        record.segments = [{'id': 1, 'start': 0, 'end': 1, 'raw': '原始转写'}]
        record.save_all()
        ident = 'a' * 24
        folder = record.directory / '.review-notes' / ident
        folder.mkdir(parents=True)
        task = {'id': ident, 'stage': '识别本节新内容', 'total_parts': 4}
        atomic_json(folder / 'coverage-batch-plan.json', {'batches': [['S1'], ['S2'], ['S3'], ['S4']]})
        atomic_json(folder / 'coverage-part-1.json', {'knowledge': [{'id': 'k2', 'sources': ['S2']}]})
        stage(record, task, folder, '识别本节新内容', status='running')
        return record, folder

    async def test_process_restart_recovers_disk_batch_and_reuses_completed_content(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record, folder = self.fixture(root)
            preserved = (folder / 'coverage-part-1.json').read_bytes()
            # Simulate termination after the next batch's durable write,
            # before updating draft/UI progress. The server can lose its
            # in-memory objects without losing either successful batch.
            atomic_json(folder / 'coverage-part-3.json', {'knowledge': []})
            record.metadata.pop('ai_note_summary_draft')
            record.save_metadata()
            with patch.object(RecordingStore, '_metadata_paths', return_value=[record.meta_path]), \
                 patch.object(storage, 'PRIVATE_ROOT', root / 'private'):
                new_store = RecordingStore()
                with new_store.service_startup():
                    restarted = new_store.find(record.id)
                    self.assertEqual(restarted.metadata['summary_job']['status'], 'interrupted')
                    self.assertEqual(restarted.metadata['summary_job']['error_kind'], 'service_restart')
                    projection = app._summary_phase_progress(restarted, {})
                    self.assertEqual((projection['completed_parts'], projection['total_parts']), (2, 4))
                    entered = asyncio.Event()
                    async def generation(ident, payload):
                        self.assertEqual(ident, record.id)
                        self.assertEqual((folder / 'coverage-part-1.json').read_bytes(), preserved)
                        entered.set()
                        await asyncio.Event().wait()
                    with patch.object(app, 'recording_store', new_store), \
                         patch.object(app.ai_preferences, 'task_config', return_value={}), \
                         patch.object(app, 'generate_library_summary', side_effect=generation):
                        app.start_summary_job(restarted, {'config': {}})
                        owner = app.background_summary_tasks[record.id]
                        await asyncio.wait_for(entered.wait(), 1)
                        owner.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await owner
                    self.assertEqual((folder / 'coverage-part-1.json').read_bytes(), preserved)
                    saved = new_store.find(record.id)
                    self.assertEqual(saved.metadata['summary_job']['status'], 'interrupted')
                    self.assertEqual(saved.metadata['summary_job']['error_kind'], 'service_interrupted')
                    self.assertEqual(checkpoint_draft(saved)['total_parts'], 4)
                    self.assertEqual(len(checkpoint_draft(saved)['parts']), 2)

    async def test_completed_stale_owners_do_not_block_continue_or_erase_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            record, folder = self.fixture(Path(raw))
            done = asyncio.create_task(asyncio.sleep(0))
            await done
            app.background_summary_tasks[record.id] = done
            app.summary_jobs[record.id] = {'task': done}
            checkpoint = record.metadata['summary_job']['checkpoint']
            try:
                with patch.object(app, 'recording_store', store_for(record)), \
                     patch.object(app.ai_preferences, 'task_config', return_value={}), \
                     patch.object(app, 'generate_library_summary', new=AsyncMock(return_value={'summary': {}})) as generate:
                    app.start_summary_job(record, {'config': {}})
                    owner = app.background_summary_tasks[record.id]
                    self.assertIsNot(owner, done)
                    await owner
                generate.assert_awaited_once()
                saved = RecordingRecord.load(record.directory)
                self.assertEqual(saved.metadata['summary_job']['checkpoint'], checkpoint)
            finally:
                app.summary_jobs.pop(record.id, None)
                app.background_summary_tasks.pop(record.id, None)

    def test_recovery_does_not_select_other_task_or_outside_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record, folder = self.fixture(root)
            record.metadata.pop('ai_note_summary_draft')
            record.metadata['summary_job']['checkpoint']['review_task'] = '../outside'
            self.assertEqual(checkpoint_draft(record), {})
            record.metadata['summary_job']['checkpoint']['review_task'] = 'b' * 24
            self.assertEqual(checkpoint_draft(record), {})
