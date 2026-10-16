"""Provider HTTP boundaries retain retry semantics and distinguish review work."""
from tests import isolation  # noqa: F401

import asyncio
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
from qwen_asr_app.agy_materials import MaterialSessionError
from tests.test_live_recovery import live_record, store_for


class WrappedProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    def record(self, path):
        record = live_record(Path(path))
        record.metadata.update(status='completed')
        record.segments = [{'id': 1, 'start': 0, 'end': 1, 'raw': '课堂内容', 'corrected': '课堂内容'}]
        record.save_all()
        return record

    async def test_real_summary_route_keeps_material_transport_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            problem = MaterialSessionError('AGY 资料会话连接中断；既有成果保留',
                                           kind='network_transport', retriable=True)
            with patch.object(app, 'recording_store', store_for(record)), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'build_note_summary', new=AsyncMock(side_effect=problem)):
                with self.assertRaises(HTTPException) as caught:
                    await app.generate_library_summary(record.id, {'config': {}})
            self.assertEqual(caught.exception.status_code, 502)
            self.assertIs(caught.exception.__cause__, problem)
            self.assertEqual(app._error_kind(caught.exception), 'network_transport')

    async def test_server_owned_job_retries_wrapped_transport_and_clears_current_error(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            attempts = 0
            async def generate(*args):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    try:
                        raise MaterialSessionError('AGY 资料会话连接中断', kind='network_transport', retriable=True)
                    except MaterialSessionError as cause:
                        raise HTTPException(502, str(cause)) from cause
                return {'summary': {'summary': '完成的笔记', 'review_status': 'passed'}}
            with patch.object(app, 'recording_store', store_for(record)), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', side_effect=generate), \
                 patch.object(app, '_automatic_retry_delay', return_value=0):
                app.start_summary_job(record, {'config': {}})
                await asyncio.wait_for(app.background_summary_tasks[record.id], 2)
            self.assertEqual(attempts, 2)
            saved = json.loads(record.meta_path.read_text())['summary_job']
            self.assertEqual(saved['status'], 'completed')
            self.assertEqual(saved['error'], '')

    async def test_wrapped_rejected_credentials_do_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            async def generate(*args):
                try:
                    raise MaterialSessionError('登录状态不可用', kind='provider_auth', retriable=False)
                except MaterialSessionError as cause:
                    raise HTTPException(502, str(cause)) from cause
            with patch.object(app, 'recording_store', store_for(record)), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', new=AsyncMock(side_effect=generate)) as request:
                app.start_summary_job(record, {'config': {}})
                await asyncio.wait_for(app.background_summary_tasks[record.id], 2)
            self.assertEqual(request.await_count, 1)
            saved = json.loads(record.meta_path.read_text())['summary_job']
            self.assertEqual(saved['status'], 'failed')
            self.assertEqual(saved['error_kind'], 'provider_auth')

    def test_saved_redacted_material_network_messages_remain_classified(self):
        self.assertEqual(app._task_error_kind('AGY 资料会话连接中断；未完成视觉审查'), 'network_transport')
        self.assertEqual(app._task_error_kind('AGY 资料会话响应超时'), 'network_read_timeout')
        self.assertNotIn(app._task_error_kind('AGY 资料会话登录状态不可用'), app.AUTO_RETRY_KINDS)

    def test_pending_scientific_review_is_not_reported_as_failed_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            record.segments[0]['correction_review'] = {'status': 'pending', 'risks': ['number']}
            record.save_all()
            with patch.object(app, 'recording_store', store_for(record)):
                task = next(x for x in app.compact_ai_tasks() if x['type'] == 'refinement')
            self.assertEqual(task['status'], 'pending')
            self.assertEqual(task['attention_kind'], 'review')
            self.assertEqual(task['review_required_blocks'], 1)
            self.assertEqual(task['unprocessed_blocks'], 0)
            self.assertEqual(task['label'], '转写待核对')
            self.assertEqual(task['error'], '')
            self.assertIn('转写已完成', task['phase'])

    async def test_full_workflow_outlives_small_operation_deadline_and_keeps_prior_error(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            record.metadata['summary_job'] = {'status': 'failed', 'error': '旧连接中断'}
            record.save_metadata()
            async def generate_library_summary(*args, **kwargs):
                await asyncio.sleep(.03)
                return {'summary': {'summary': '完成', 'review_status': 'passed'}}
            guarded = app.recording_operation(generate_library_summary)
            with patch.object(app, 'recording_store', store_for(record)), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', guarded), \
                 patch.object(app, 'SUMMARY_SMALL_OPERATION_TIMEOUT_SECONDS', .005), \
                 patch.object(app, 'SUMMARY_WORKFLOW_TIMEOUT_SECONDS', .2):
                app.start_summary_job(record, {'config': {}})
                await asyncio.wait_for(app.background_summary_tasks[record.id], 1)
            saved = json.loads(record.meta_path.read_text())['summary_job']
            self.assertEqual(saved['status'], 'completed')
            self.assertEqual(saved['previous_error'], '旧连接中断')
            self.assertEqual(saved['error'], '')
            self.assertNotIn(record.id, app.summary_jobs)
            self.assertNotIn(record.id, app.record_operations)

    async def test_full_workflow_still_has_finite_timeout_and_drains_owner(self):
        stopped = asyncio.Event()
        async def generate_library_summary(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        guarded = app.recording_operation(generate_library_summary)
        with patch.object(app, 'SUMMARY_WORKFLOW_TIMEOUT_SECONDS', .01):
            with self.assertRaises(HTTPException) as caught:
                await guarded('bounded-workflow')
        self.assertEqual(caught.exception.status_code, 504)
        self.assertTrue(stopped.is_set())
        self.assertNotIn('bounded-workflow', app.summary_jobs)
        self.assertNotIn('bounded-workflow', app.record_operations)

    def test_current_latex_batch_progress_replaces_old_knowledge_plan_count(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            ident = 'a' * 24
            folder = record.directory / '.review-notes' / ident
            (folder / 'latex').mkdir(parents=True)
            (folder / 'note.md').write_text('完整参考正文')
            draft = {'phase': 'LaTeX 排版', 'parts': {str(i): '旧知识计划' for i in range(6)},
                     'total_parts': 6, 'review_task': ident}
            checkpoint = {'binding': {'markdown_sha256': hashlib.sha256((folder / 'note.md').read_bytes()).hexdigest()},
                          'total_batches': 9, 'completed_batches': 2}
            (folder / 'latex' / 'batch-progress.json').write_text(json.dumps(checkpoint))
            progress = app._summary_phase_progress(record, draft)
            self.assertEqual(progress, {'phase': 'LaTeX 排版 · 第 3/9 批', 'completed_parts': 2, 'total_parts': 9})
            self.assertEqual(draft['total_parts'], 6)  # Display only, never rewrite owner evidence.
            (folder / 'note.md').write_text('用户后来修改的参考正文')
            self.assertEqual(app._summary_phase_progress(record, draft)['total_parts'], 6)

    def test_invalid_or_escaped_batch_progress_cannot_override_current_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            draft = {'phase': '独立审查', 'parts': {}, 'total_parts': 0, 'review_task': '../private'}
            self.assertEqual(app._summary_phase_progress(record, draft)['phase'], '独立审查')
            draft.update(phase='LaTeX 排版', review_task='a' * 24)
            folder = record.directory / '.review-notes' / draft['review_task']
            (folder / 'latex').mkdir(parents=True)
            (folder / 'note.md').write_text('正文')
            for bad in [{'total_batches': True, 'completed_batches': 0},
                        {'total_batches': 9, 'completed_batches': 10},
                        {'total_batches': 9, 'completed_batches': -1}]:
                (folder / 'latex' / 'batch-progress.json').write_text(json.dumps(bad))
                self.assertEqual(app._summary_phase_progress(record, draft)['total_parts'], 0)

    def test_visual_review_uses_page_checkpoint_not_completed_writing_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory)
            ident = 'b' * 24
            folder = record.directory / '.review-notes' / ident
            folder.mkdir(parents=True)
            draft = {'phase': '独立审查', 'parts': {'1': '旧写作批次'}, 'total_parts': 1,
                     'review_task': ident}
            checkpoint = {'id': ident, 'review_total_pages': 38, 'reviewed_pages': 6,
                          'pdf_sha256': 'c' * 64}
            (folder / 'task.json').write_text(json.dumps(checkpoint))
            self.assertEqual(app._summary_phase_progress(record, draft),
                             {'phase': '独立审查 · 已查看 6/38 页', 'completed_parts': 6,
                              'total_parts': 38, 'unit': '页','review_round':1,'review_batches':[]})
            checkpoint['reviewed_pages'] = 39
            (folder / 'task.json').write_text(json.dumps(checkpoint))
            self.assertEqual(app._summary_phase_progress(record, draft)['total_parts'], 1)
