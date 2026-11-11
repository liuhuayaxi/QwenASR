"""Fault injection at course boundaries: derived AI cannot veto durable ASR."""
from tests import isolation  # noqa: F401

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
from qwen_asr_app.course_stream import CourseStreamJob, CourseStreamManager
from qwen_asr_app.storage import RecordingRecord
from tests.test_course_stream import make_record, until


def runtime(transcribe=None):
    return SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
        transcribe=transcribe or AsyncMock(return_value=('已保存课堂原文', 'Chinese', .01)))


class CourseIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_correction_checkpoints_preserve_queued_note_and_explicit_pause(self):
        for pause in (False, True):
            with self.subTest(pause=pause), tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
                record=make_record(Path(raw));record.metadata.update(status='completed',summary_job={'status':'paused'})
                record.save_all()
                store=SimpleNamespace(find=lambda ident:RecordingRecord.load(record.directory))
                for name,value in [('recording_store',store),('background_ai_tasks',{}),('background_ai_records',{}),
                    ('background_summary_tasks',{}),('summary_jobs',{}),('record_operations',set()),
                    ('running_batch_ids',set()),('finalization_tasks',{}),('summary_queue_order',[])]:
                    stack.enter_context(patch.object(app,name,value))
                stack.enter_context(patch.object(app.course_stream_manager,'jobs',{}))
                stack.enter_context(patch.object(app.ai_preferences,'task_config',side_effect=lambda config,**kwargs:config or {}))
                generated=stack.enter_context(patch.object(app,'generate_library_summary',AsyncMock(return_value={'summary':{'summary':'完整笔记'}})))
                entered,release=asyncio.Event(),asyncio.Event()
                async def correct():
                    entered.set();await release.wait()
                    record.save_all()
                app._start_aux_ai_job(record,'refinement',correct)
                correction=app.background_ai_tasks['refinement:'+record.id]
                await entered.wait()
                app.start_summary_job(store.find(record.id),{'config':{}})
                note=app.background_summary_tasks[record.id]
                try:
                    record.save_all()
                    self.assertEqual(store.find(record.id).metadata['summary_job']['status'],'queued')
                    if pause:
                        await app.ai_task_action({'recording_id':record.id,'type':'summary','action':'pause'})
                        await asyncio.gather(note,return_exceptions=True)
                        record.save_all()
                        self.assertEqual(store.find(record.id).metadata['summary_job']['status'],'paused')
                        self.assertFalse(correction.done())
                    release.set();await correction
                    if not pause:
                        await note
                        self.assertEqual(store.find(record.id).metadata['summary_job']['status'],'completed')
                        generated.assert_awaited_once()
                    self.assertFalse(app.background_ai_records)
                finally:
                    release.set()
                    for task in (note,correction):
                        if not task.done():task.cancel()
                    await asyncio.gather(note,correction,return_exceptions=True)

    async def test_paused_correction_stays_paused_across_asr_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            record = make_record(Path(raw))
            entered = asyncio.Event()

            async def blocked(*args):
                entered.set()
                await asyncio.Event().wait()

            model = runtime()
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', blocked):
                job = CourseStreamJob(record, {'ai_correction': True}, model, set())
                job.start()
                await job.accept(0, bytes(32000), {})
                await entered.wait()
                await job.pause_correction()
                await until(lambda: job.state.get('completed_chunks') == 1)
                await job.stop(paused=True)
            saved = RecordingRecord.load(record.directory)
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', AsyncMock()) as correction:
                resumed = CourseStreamJob(saved, {'ai_correction': True}, model, set())
                resumed.start()
                await resumed.accept(1, bytes(32000), {})
                await resumed.finish_input(2)
                await asyncio.wait_for(resumed.task, 3)
                correction.assert_not_awaited()
            self.assertEqual(model.transcribe.await_count, 2)
            self.assertEqual(saved.metadata['status'], 'completed')
            self.assertEqual(saved.metadata['ai_jobs']['refinement']['status'], 'paused')
            self.assertEqual(len(saved.segments), 2)

    async def test_blocked_correction_allows_archive_other_asr_and_independent_pause(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = make_record(root)
            entered = asyncio.Event()

            async def blocked(*args):
                entered.set()
                await asyncio.Event().wait()

            model, busy = runtime(), set()
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', blocked), \
                    patch.object(app, 'running_batch_ids', busy), \
                    patch.object(app.course_stream_manager, 'jobs', {}):
                job = CourseStreamJob(record, {'ai_correction': True}, model, busy)
                app.course_stream_manager.jobs[record.id] = job
                job.start()
                try:
                    await job.accept(0, bytes(32000), {})
                    await asyncio.wait_for(entered.wait(), 1)
                    await job.accept(1, bytes(32000), {})
                    await job.finish_input(2)
                    await until(lambda: record.metadata['status'] == 'completed')
                    saved = RecordingRecord.load(record.directory)
                    self.assertEqual(len(saved.segments), 2)
                    self.assertTrue(saved.audio_path.exists())
                    self.assertTrue(job.snapshot()['ai_in_background'])
                    self.assertFalse(job.task.done())
                    self.assertEqual(model.active_jobs, 0)

                    other = make_record(root)
                    next_job = CourseStreamJob(other, {}, model, busy)
                    next_job.start()
                    await next_job.accept(0, bytes(32000), {})
                    await next_job.finish_input(1)
                    await asyncio.wait_for(next_job.task, 3)
                    self.assertEqual(other.metadata['status'], 'completed')

                    with patch.object(app.recording_store, 'find', return_value=record):
                        result = await app.ai_task_action({'recording_id': record.id,
                            'type': 'refinement', 'action': 'pause'})
                    self.assertTrue(result['asr_continues'])
                    await asyncio.wait_for(job.task, 2)
                    saved = RecordingRecord.load(record.directory)
                    self.assertEqual(saved.metadata['status'], 'completed')
                    self.assertEqual(saved.metadata['ai_jobs']['refinement']['status'], 'paused')
                    self.assertEqual(len(saved.segments), 2)
                    self.assertNotIn(record.id, busy)
                finally:
                    await job.stop()

    async def test_correction_consumer_failure_does_not_interrupt_later_asr(self):
        with tempfile.TemporaryDirectory() as raw:
            record = make_record(Path(raw))
            model = runtime()
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment',
                       AsyncMock(side_effect=RuntimeError('synthetic correction failure'))):
                job = CourseStreamJob(record, {'ai_correction': True}, model, set())
                job.start()
                await job.accept(0, bytes(32000), {})
                await until(lambda: job.state.get('correction_deferred') or (job.pipeline and job.pipeline.task.done()))
                await job.accept(1, bytes(32000), {})
                await job.finish_input(2)
                await asyncio.wait_for(job.task, 3)
            self.assertEqual(model.transcribe.await_count, 2)
            self.assertEqual(record.metadata['status'], 'completed')
            self.assertEqual(len(record.segments), 2)
            self.assertTrue(record.audio_path.exists())
            self.assertTrue(job.snapshot()['correction_deferred'])
            self.assertIn('synthetic correction failure', job.snapshot()['correction_error'])

    async def test_cancelled_correction_consumer_does_not_cancel_asr_owner(self):
        with tempfile.TemporaryDirectory() as raw:
            record = make_record(Path(raw))
            entered = asyncio.Event()

            async def blocked(*args):
                entered.set()
                await asyncio.Event().wait()

            model = runtime()
            with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', blocked):
                job = CourseStreamJob(record, {'ai_correction': True}, model, set())
                job.start()
                await job.accept(0, bytes(32000), {})
                await entered.wait()
                job.pipeline.task.cancel()
                await asyncio.gather(job.pipeline.task, return_exceptions=True)
                await job.accept(1, bytes(32000), {})
                await job.finish_input(2)
                await asyncio.wait_for(job.task, 3)
            self.assertEqual(model.transcribe.await_count, 2)
            self.assertEqual(record.metadata['status'], 'completed')
            self.assertTrue(job.snapshot()['correction_deferred'])

    async def test_download_failure_drains_accepted_chunks_and_keeps_resume_cursor(self):
        with tempfile.TemporaryDirectory() as raw:
            record = make_record(Path(raw))
            entered, release = asyncio.Event(), asyncio.Event()

            async def transcribe(*args, **kwargs):
                entered.set()
                await release.wait()
                return '断网前已接收的原文', 'Chinese', .01

            model = runtime(AsyncMock(side_effect=transcribe))
            job = CourseStreamJob(record, {}, model, set())
            job.start()
            try:
                for i in range(2):
                    await job.accept(i, bytes(32000), {'source': 0, 'samples': (i + 1) * 16000})
                await entered.wait()
                job.interrupt_input('synthetic media disconnect')
                self.assertFalse(job.task.done())
                release.set()
                await asyncio.wait_for(job.task, 2)
                saved = RecordingRecord.load(record.directory)
                self.assertEqual(model.transcribe.await_count, 2)
                self.assertEqual(len(saved.segments), 2)
                self.assertEqual(saved.metadata['status'], 'interrupted')
                self.assertEqual(job.snapshot()['resume']['samples'], 32000)
                self.assertIn('media disconnect', job.snapshot()['error'])
                self.assertFalse(job.snapshot()['download_complete'])
                self.assertTrue((record.directory / '.course-chunks' / '00001.pcm').exists())
            finally:
                release.set()
                await job.stop()

    async def test_resume_takes_over_manual_correction_after_its_checkpoint_is_saved(self):
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            root = Path(raw)
            record = make_record(root)
            record.metadata['status'] = 'interrupted'
            record.save_all()
            store = SimpleNamespace(paths={record.id: record.directory},
                list_records=lambda: [RecordingRecord.load(record.directory)],
                find=lambda ident: RecordingRecord.load(record.directory))
            model, busy = runtime(), set()
            manager = CourseStreamManager(model, store,
                SimpleNamespace(storage_root=root, add_category=lambda v: v), busy,
                is_record_busy=app.recording_is_busy, prepare_resume=app.prepare_course_resume)
            for name, value in [('background_ai_tasks', {}), ('record_operations', set()),
                                ('background_summary_tasks', {}), ('running_batch_ids', busy),
                                ('finalization_tasks', {})]:
                stack.enter_context(patch.object(app, name, value))
            stack.enter_context(patch('qwen_asr_app.course_stream.ai_preferences.task_config', side_effect=lambda config, **kwargs: config))
            started = asyncio.Event()

            async def manual():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    latest = RecordingRecord.load(record.directory)
                    latest.metadata['manual_checkpoint_saved'] = True
                    latest.save_metadata()

            task = asyncio.create_task(manual())
            app.background_ai_tasks['refinement:' + record.id] = task
            await started.wait()
            result = await manager.create({'key': 'test', 'duration': 6, 'sources': [], 'config': {}})
            resumed = manager.jobs[result['id']]
            try:
                self.assertTrue(task.cancelled())
                self.assertTrue(resumed.record.metadata['manual_checkpoint_saved'])
                self.assertEqual(result['status'], 'processing')
            finally:
                await resumed.stop()

    async def test_waiting_note_does_not_take_a_slot_or_cancel_correction(self):
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            root = Path(raw)
            records = [make_record(root) for _ in range(2)]
            paths = {record.id: record.directory for record in records}
            store = SimpleNamespace(find=lambda ident: RecordingRecord.load(paths[ident]))
            model, busy = runtime(), set()
            entered = asyncio.Event()

            async def blocked(*args):
                entered.set()
                await asyncio.Event().wait()

            stack.enter_context(patch('qwen_asr_app.batch_pipeline.correct_batch_segment', blocked))
            stack.enter_context(patch.object(app, 'recording_store', store))
            for name, value in [('background_ai_tasks', {}), ('background_summary_tasks', {}),
                                ('summary_jobs', {}), ('record_operations', set()),
                                ('running_batch_ids', busy), ('SUMMARY_QUEUE_CONCURRENCY', 1),
                                ('summary_queue_order', [])]:
                stack.enter_context(patch.object(app, name, value))
            stack.enter_context(patch.object(app.ai_preferences, 'task_config', side_effect=lambda config, **kwargs: config))
            generated = []

            async def generate(ident, payload):
                generated.append(ident)
                return {'summary': {'summary': '合成笔记', 'abstract': '摘要'}}

            stack.enter_context(patch.object(app, 'generate_library_summary', generate))
            job = CourseStreamJob(records[0], {'ai_correction': True}, model, busy)
            stack.enter_context(patch.object(app.course_stream_manager, 'jobs', {records[0].id: job}))
            job.start()
            try:
                await job.accept(0, bytes(32000), {})
                await entered.wait()
                await job.finish_input(1)
                await until(lambda: records[0].metadata['status'] == 'completed')
                records[1].metadata['status'] = 'completed'
                records[1].save_all()
                for record in records:
                    app.start_summary_job(store.find(record.id), {'config': {}})
                pending = app.background_summary_tasks[records[0].id]
                other = app.background_summary_tasks[records[1].id]
                await asyncio.wait_for(other, 2)
                self.assertEqual(generated, [records[1].id])
                self.assertFalse(pending.done())
                self.assertEqual(records[0].metadata['summary_job']['waiting_for'], 'correction')
                await app.ai_task_action({'recording_id': records[0].id, 'type': 'summary', 'action': 'pause'})
                await asyncio.gather(pending, return_exceptions=True)
                self.assertFalse(job.task.done())
                self.assertTrue(job.pipeline)
                self.assertEqual(records[0].metadata['summary_job']['status'], 'paused')
                app.start_summary_job(store.find(records[0].id), {'config': {}})
                resumed_note = app.background_summary_tasks[records[0].id]
                await job.pause_correction()
                await asyncio.wait_for(job.task, 2)
                await asyncio.wait_for(resumed_note, 2)
                self.assertEqual(generated, [records[1].id, records[0].id])
                self.assertEqual(RecordingRecord.load(records[0].directory).metadata['summary_job']['status'], 'completed')
            finally:
                await job.stop()
                tasks = list(app.background_summary_tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
