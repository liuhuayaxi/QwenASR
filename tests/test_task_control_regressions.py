from tests import isolation  # noqa: F401

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

import app
from qwen_asr_app.live_session import TranscriptionSession
from qwen_asr_app.task_center import task_center_router
from tests.test_live_recovery import live_record, store_for
from tests.test_task_aggregation import task


class TaskControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_and_cancelled_periodic_work_stays_stopped_until_continue(self):
        for kind, periodic in [('live_summary', 'summary'), ('refinement', 'review')]:
            for action in ['pause', 'cancel']:
                with self.subTest(kind=kind, action=action), tempfile.TemporaryDirectory() as raw:
                    record = live_record(Path(raw))
                    record.metadata['status'] = 'completed'
                    store = store_for(record)
                    store.find = lambda ident: record
                    session = TranscriptionSession(SimpleNamespace(send_json=AsyncMock()), SimpleNamespace(), store)
                    worker = AsyncMock()
                    setattr(session, 'run_periodic_summaries' if periodic == 'summary' else 'run_periodic_reviews', worker)
                    runner = '_run_live_summary_job' if kind == 'live_summary' else '_run_refinement_job'
                    with patch.object(app, 'recording_store', store), \
                         patch.dict(app.live_sessions, {record.id: session}, clear=True), \
                         patch.object(app, '_saved_ai_task_config', return_value={}), \
                         patch.object(app, 'require_recording_available'), \
                         patch.object(app, 'schedule_memory_cleanup'), \
                         patch.object(app, runner, new=AsyncMock()):
                        record.metadata.setdefault('ai_jobs', {})[kind] = {'status': 'pending'}
                        await app.ai_task_action({'recording_id': record.id, 'type': kind, 'action': action})
                        session.schedule_periodic(periodic, record, {})
                        self.assertFalse(session.periodic_tasks)
                        worker.assert_not_awaited()
                        result = await app.ai_task_action({'recording_id': record.id, 'type': kind, 'action': 'continue'})
                        self.assertTrue(result['accepted'])
                        await asyncio.gather(*app.background_ai_tasks.values())
                        session.schedule_periodic(periodic, record, {})
                        await asyncio.gather(*session.periodic_tasks.values())
                        worker.assert_awaited_once()

    async def test_continue_waits_for_cancel_cleanup_or_returns_reason(self):
        for timeout in [False, True]:
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as raw:
                record = live_record(Path(raw))
                record.metadata['status'] = 'completed'
                started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                async def runner():
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaning.set()
                        await release.wait()
                with patch.object(app, 'recording_store', SimpleNamespace(find=lambda ident: record)), \
                     patch.object(app, 'require_recording_available'), \
                     patch.object(app, 'schedule_memory_cleanup'), \
                     patch.object(app, '_saved_ai_task_config', return_value={}), \
                     patch.object(app, '_run_live_summary_job', new=AsyncMock()):
                    app._start_aux_ai_job(record, 'live_summary', runner)
                    await started.wait()
                    await app.ai_task_action({'recording_id': record.id, 'type': 'live_summary', 'action': 'pause'})
                    await cleaning.wait()
                    if timeout:
                        context = patch.object(app.asyncio, 'wait', new=AsyncMock(return_value=(set(), set())))
                    else:
                        asyncio.get_running_loop().call_soon(release.set)
                        context = patch.object(app, 'MAX_AUTOMATIC_RETRIES', app.MAX_AUTOMATIC_RETRIES)
                    try:
                        with context:
                            result = await app.ai_task_action({'recording_id': record.id, 'type': 'live_summary', 'action': 'continue'})
                        self.assertEqual(result['accepted'], not timeout)
                        if timeout:
                            self.assertTrue(result['reason'])
                    finally:
                        release.set()
                        await asyncio.gather(*app.background_ai_tasks.values(), return_exceptions=True)

    async def test_bulk_counts_rejected_owner_as_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            courses = SimpleNamespace(store=SimpleNamespace(settings=SimpleNamespace(
                categories=[], storage_root=root), list=lambda category: []), tasks={})
            api = FastAPI()
            with patch('qwen_asr_app.task_center.PRIVATE_ROOT', root), \
                 patch('qwen_asr_app.task_center.discover_courses'), \
                 patch('qwen_asr_app.task_center.course_tasks', return_value=[]):
                api.include_router(task_center_router(lambda **kwargs: [task('summary:lesson', 'paused', recording_title='lesson')],
                    AsyncMock(return_value={'accepted': False, 'reason': 'still stopping'}), courses, SimpleNamespace(jobs={})))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
                    response = await client.post('/api/tasks/bulk-action', headers={'X-QwenASR-Client': 'task-center'},
                        json={'action': 'continue', 'items': [{'id': 'summary:lesson'}]})
                result = response.json()
                self.assertEqual(result['accepted'], 0)
                self.assertFalse(result['results'][0]['ok'])
                self.assertEqual(result['results'][0]['error'], 'still stopping')
