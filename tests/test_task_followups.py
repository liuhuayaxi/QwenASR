"""ASR lifetime and durable summary follow-ups, using disposable records."""
from tests import isolation  # noqa: F401

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

import app
from qwen_asr_app.model_runtime import ModelManager
from qwen_asr_app.note_content import transcript_blocks
from qwen_asr_app.course_stream import CourseStreamJob
from tests.test_course_stream import make_record, until
from tests.test_live_recovery import live_record, store_for
from tests.test_reader_pipeline import record_at


class ModelLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_releases_model_before_ai_and_only_its_own_lease(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as directory:
                record = record_at(Path(directory), status='interrupted', ai_note_summary={})
                record.audio_path.touch()
                record.complete = lambda: record.metadata.update(status='completed')
                released, finish_ai = asyncio.Event(), asyncio.Event()
                manager = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None,
                    schedule_unload=released.set, load=AsyncMock(),
                    transcribe=AsyncMock(return_value=('课堂文字', 'Chinese', .1)))
                async def correct(rec, segment, config):
                    await finish_ai.wait()
                    segment.update(corrected='精修文字', ai_pending=False)
                    return segment
                async def rewrite(rec, config):
                    yield {'type': 'ai_global_complete', 'global_rewrite': {'complete': True}}
                with patch.object(app, 'model_manager', manager), \
                     patch.object(app, 'decode_audio_file', return_value=np.zeros(16000)), \
                     patch.object(app, 'vad_split_ranges', return_value=[(0, 16000)]), \
                     patch('qwen_asr_app.batch_pipeline.correct_batch_segment', correct), \
                     patch.object(app, 'global_rewrite_events', rewrite):
                    async def collect():
                        return [json.loads(event) async for event in app.batch_transcription_stream(
                            None, 'test.wav', {'ai_correction': True, 'translation': 'off'}, existing_record=record)]
                    task = asyncio.create_task(collect())
                    try:
                        await asyncio.wait_for(released.wait(), 1)
                        self.assertEqual(manager.active_jobs, 0)
                        self.assertFalse(task.done())
                        # Another recognition can start after this job releases
                        # ASR; later AI completion/cancellation must leave it owned.
                        manager.active_jobs = 1
                        if cancel:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        else:
                            finish_ai.set()
                            events = await asyncio.wait_for(task, 2)
                            self.assertEqual(events[-1]['type'], 'complete')
                        self.assertEqual(manager.active_jobs, 1)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_course_unloads_model_while_ai_keeps_record_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            record = make_record(Path(directory))
            manager = ModelManager(Path(directory))
            manager.worker = SimpleNamespace(is_alive=lambda: True)
            manager.transcribe = AsyncMock(return_value=('课堂文字', 'Chinese', .1))
            released, ai_started, finish_ai = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def unload():
                manager.worker = None
                released.set()
            manager._unload_worker = unload
            schedule = manager.schedule_unload
            manager.schedule_unload = lambda: schedule(delay=.01)
            async def correct(rec, segment, config):
                ai_started.set()
                await finish_ai.wait()
                segment.update(corrected='精修文字', ai_pending=False)
                return segment
            busy = set()
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', correct):
                job = CourseStreamJob(record, {'ai_correction': True}, manager, busy)
                job.start()
                try:
                    await job.accept(0, bytes(3 * 16000 * 2), {})
                    await asyncio.wait_for(ai_started.wait(), 1)
                    self.assertEqual(manager.active_jobs, 1)
                    self.assertFalse(released.is_set())
                    await job.finish_input(1)
                    await asyncio.wait_for(released.wait(), 1)
                    self.assertEqual(manager.active_jobs, 0)
                    self.assertIn(record.id, busy)
                    self.assertFalse(job.task.done())
                    finish_ai.set()
                    await asyncio.wait_for(job.task, 5)
                    self.assertEqual(record.metadata['status'], 'completed')
                    self.assertEqual(record.segments[0]['corrected'], '精修文字')
                    self.assertTrue(record.audio_path.exists())
                    self.assertEqual(manager.active_jobs, 0)
                finally:
                    await job.stop()
                    await manager.close()

    async def test_cancel_during_ai_does_not_release_another_asr_job(self):
        with tempfile.TemporaryDirectory() as directory:
            record = make_record(Path(directory))
            manager = SimpleNamespace(active_jobs=1, cancel_unload=lambda: None,
                schedule_unload=lambda: None,
                transcribe=AsyncMock(return_value=('课堂文字', 'Chinese', .1)))
            started = asyncio.Event()
            async def correct(*args):
                started.set()
                await asyncio.Event().wait()
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', correct):
                job = CourseStreamJob(record, {'ai_correction': True}, manager, set())
                job.start()
                await job.accept(0, bytes(3 * 16000 * 2), {})
                await asyncio.wait_for(started.wait(), 1)
                await job.finish_input(1)
                await until(lambda: manager.active_jobs == 1)
                await job.stop()
            self.assertEqual(manager.active_jobs, 1)
            self.assertEqual(record.metadata['status'], 'interrupted')


def saved_summary_record(directory):
    record = live_record(Path(directory))
    record.metadata.update(status='completed',
        summary_job={'status': 'failed', 'started_at': '2026-10-11T17:31:36+08:00',
                     'error': '生成耗时超过 30 分钟'},
        ai_note_summary={'summary': '# 已保存的完整笔记', 'abstract': '',
                         'updated_at': '2026-10-11T17:58:22+08:00'},
        ai_note_summary_draft={'phase': '录音库短摘要', 'parts': {'0': '完整笔记'}, 'total_parts': 1})
    record.segments = [{'id': 1, 'raw': '课堂文字', 'corrected': '课堂文字', 'start': 0, 'end': 1}]
    record.save_all()
    return record


class SummaryFollowupTests(unittest.IsolatedAsyncioTestCase):
    def test_feed_shows_preserved_note_as_partial_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            record = saved_summary_record(directory)
            with patch.object(app, 'recording_store', store_for(record)):
                task = next(t for t in app.compact_ai_tasks() if t['type'] == 'summary')
            self.assertEqual(task['status'], 'completed_with_errors')
            self.assertEqual(task['resume_stage'], 'abstract')
            self.assertIn('详细笔记已保存', task['phase'])
            self.assertEqual(task['completed'], 1)
            self.assertIn('continue', task['actions'])
            self.assertEqual(record.metadata['summary_job']['status'], 'failed')

    def test_old_note_cannot_turn_failed_regeneration_into_partial_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            record = saved_summary_record(directory)
            record.metadata['ai_note_summary']['updated_at'] = '2026-10-10T12:00:00+08:00'
            self.assertFalse(app._summary_abstract_pending(record))
            record.metadata.pop('ai_note_summary_draft')
            self.assertFalse(app._summary_abstract_pending(record))

    async def test_continue_and_retry_only_fill_abstract_without_rewriting_note(self):
        for action in ('continue', 'retry'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                record = saved_summary_record(directory)
                store = store_for(record)
                original = record.metadata['ai_note_summary']['summary']
                async def abstract(rec, config):
                    rec.metadata['ai_note_summary']['abstract'] = '已补齐的短摘要'
                    rec.save_all()
                    return rec.metadata['ai_note_summary']
                with patch.object(app, 'recording_store', store), \
                     patch.object(app, '_saved_ai_task_config', return_value={}), \
                     patch.object(app.ai_preferences, 'task_config', return_value={}), \
                     patch.object(app, 'regenerate_note_abstract', side_effect=abstract) as fill, \
                     patch.object(app, 'build_note_summary', new=AsyncMock()) as regenerate:
                    self.assertTrue((await app.ai_task_action(
                        {'recording_id': record.id, 'type': 'summary', 'action': action}))['accepted'])
                    await asyncio.wait_for(app.background_summary_tasks[record.id], 2)
                    fill.assert_awaited_once()
                    regenerate.assert_not_awaited()
                saved = store.find(record.id)
                self.assertEqual(saved.metadata['summary_job']['status'], 'completed')
                self.assertEqual(saved.metadata['ai_note_summary']['summary'], original)
                self.assertNotIn('ai_note_summary_draft', saved.metadata)

    async def test_continue_and_retry_request_a_fresh_pdf_review(self):
        for action in ('continue', 'retry'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                record = record_at(Path(directory), ai_note_summary={'summary': '已保存笔记', 'pdf_ready': False},
                                   summary_job={'status': 'completed_with_errors', 'resume_stage': 'pdf'})
                record.save_all()
                store = store_for(record)
                async def finish_review(record, config):
                    self.assertIs(config.get('_fresh_review'), True)
                    record.metadata['ai_note_summary']['pdf_ready'] = True
                    record.save_metadata()
                    return {'status': 'passed'}
                with patch.object(app, 'recording_store', store), \
                     patch.object(app, '_saved_ai_task_config', return_value={}), \
                     patch.object(app.ai_preferences, 'task_config', return_value={'note_review_enabled': True}), \
                     patch('qwen_asr_app.review_notes.upgrade_pdf', side_effect=finish_review) as review:
                    result = await app.ai_task_action(
                        {'recording_id': record.id, 'type': 'summary', 'action': action})
                    self.assertTrue(result['accepted'])
                    await asyncio.wait_for(app.background_summary_tasks[record.id], 2)
                    review.assert_awaited_once()
                self.assertEqual(store.find(record.id).metadata['summary_job']['status'], 'completed')

    async def test_failed_abstract_stays_visible_after_detailed_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            record = saved_summary_record(directory)
            store = store_for(record)
            result = {'summary': {'summary': '笔记正文', 'abstract_error': '连接中断'}}
            with patch.object(app, 'recording_store', store), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', new=AsyncMock(return_value=result)):
                app.start_summary_job(record, {'config': {}})
                await app.background_summary_tasks[record.id]
            saved = store.find(record.id)
            self.assertEqual(saved.metadata['summary_job']['status'], 'completed_with_errors')
            self.assertEqual(saved.metadata['summary_job']['resume_stage'], 'abstract')
            self.assertEqual(saved.metadata['summary_job']['error'], '连接中断')


class ParallelRecoveryTests(unittest.IsolatedAsyncioTestCase):

    async def test_manual_refinement_fans_out_and_preserves_every_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            record = record_at(Path(directory), ai_note_summary={})
            record.segments = [{'id': i, 'raw': f'课堂原文{i}', 'start': i - 1, 'end': i} for i in range(1, 31)]
            config = {'_parallel_refinement': True, 'ai': {'correction_provider': {
                'transport': 'agy_cli', 'cli_model': 'gemini-3.8-flash-high',
                'cli_effort': 'high', 'concurrency': 2, 'context_window': 256000}}}
            running = peak = 0
            two_running, finish = asyncio.Event(), asyncio.Event()
            async def refine(segments, *args, **kwargs):
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                if running == 2:
                    two_running.set()
                try:
                    await finish.wait()
                    await asyncio.sleep((30 - segments[0]['id']) / 10000)
                    return [{'segment_ids': [s['id']], 'corrected': f"精修{s['id']}"} for s in segments]
                finally:
                    running -= 1
            with patch.object(app, 'estimate_tokens', return_value=1000), \
                 patch.object(app, 'polish_timestamped_segments_resilient', refine):
                task = asyncio.create_task(app._run_refinement_job_unlocked(record, config))
                try:
                    await asyncio.wait_for(two_running.wait(), 1)
                    self.assertEqual(peak, 2)
                    finish.set()
                    await asyncio.wait_for(task, 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            blocks = transcript_blocks(record)
            self.assertEqual([b['text'] for b in blocks], [f'精修{i}' for i in range(1, 31)])
            self.assertTrue(all(b['refined'] for b in blocks))
            self.assertEqual(record.metadata['ai_jobs']['refinement']['completed'], 30)
            self.assertEqual(record.metadata['ai_jobs']['refinement']['total'], 30)
            self.assertEqual(record.metadata['ai_jobs']['refinement']['total_batches'],2)
