"""Request events are observable before completion, without exposing content."""
from tests import isolation  # noqa: F401

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import app
from qwen_asr_app.ai import AIRequestError, request_ai_text
from qwen_asr_app.ai_activity import track_ai_activity, track_ai_request, task_ai_activity, report_ai_activity
from qwen_asr_app.agy_cli import request_text
from tests.test_reader_pipeline import record_at
from tests.test_live_recovery import store_for


PROVIDER = {'transport': 'agy_cli', 'cli_model': 'gemini-3.8-flash-high', 'cli_effort': 'high'}
CONFIG = {'ai': {'correction_provider': PROVIDER}}


def process():
    stdout = asyncio.StreamReader()
    value = SimpleNamespace(returncode=None, stdout=stdout,
        stderr=SimpleNamespace(read=AsyncMock(return_value=b'diagnostic' * 100_000)),
        stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()), kill=Mock())
    async def wait():
        value.returncode = 0
        return 0
    value.wait = AsyncMock(side_effect=wait)
    return value


def event(child, data):
    child.stdout.feed_data((json.dumps(data, ensure_ascii=False) + '\n').encode())


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def test_finished_output_is_not_counted_as_current_request_output(self):
        with patch('qwen_asr_app.ai_activity.time.monotonic',return_value=100) as clock,track_ai_activity('stages'):
            with track_ai_request():report_ai_activity('receiving',output_chars=90000)
            clock.return_value=200
            with track_ai_request():
                clock.return_value=215
                snapshot=task_ai_activity('stages')
                self.assertEqual(snapshot['output_chars'],90000)
                self.assertEqual(snapshot['current_output_chars'],0)
                self.assertEqual(snapshot['request_elapsed_seconds'],15)
                report_ai_activity('receiving',output_chars=50)
                self.assertEqual(task_ai_activity('stages')['current_output_chars'],50)

    def setUp(self):
        fixture = patch('qwen_asr_app.agy_cli.executable', return_value='/test/agy')
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_a_new_connection_does_not_reset_a_silent_output_streams_age(self):
        with patch('qwen_asr_app.ai_activity.time.monotonic', return_value=100) as clock, \
             track_ai_activity('ages'), track_ai_request():
            report_ai_activity('receiving', output_chars=10)
            clock.return_value = 120
            with track_ai_request():
                report_ai_activity('connecting')
                snapshot = task_ai_activity('ages')
                self.assertEqual(snapshot['phase'], 'receiving')
                self.assertEqual(snapshot['idle_seconds'], 20)
                self.assertEqual(snapshot['active_requests'], 2)

    async def test_stream_updates_are_visible_before_result_and_counts_do_not_double(self):
        child = process()
        with patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', return_value=child), \
             track_ai_activity('synthetic'):
            request = asyncio.create_task(request_ai_text(instructions='synthetic', payload={},
                config=CONFIG, mode='correction', max_output_tokens=100))
            try:
                await asyncio.sleep(0)
                self.assertEqual(task_ai_activity('synthetic')['phase'], 'connecting')
                event(child, {'event': 'init', 'init': {'model': 'synthetic'}})
                await asyncio.sleep(.01)
                self.assertEqual(task_ai_activity('synthetic')['phase'], 'processing')
                event(child, {'event': 'step_update', 'step_update': {
                    'step_type': 'agent_response', 'text_delta': '输出正文'}})
                await asyncio.sleep(.01)
                snapshot = task_ai_activity('synthetic')
                self.assertEqual(snapshot['phase'], 'receiving')
                self.assertEqual(snapshot['output_chars'], 4)
                self.assertFalse(request.done())
                self.assertNotIn('输出正文', json.dumps(snapshot, ensure_ascii=False))
                event(child, {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '输出正文'}})
                child.stdout.feed_eof()
                self.assertEqual(await request, '输出正文')
                self.assertEqual(task_ai_activity('synthetic')['output_chars'], 4)
                self.assertEqual(task_ai_activity('synthetic')['phase'], 'checking')
                child.kill.assert_not_called()
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        self.assertIsNone(task_ai_activity('synthetic'))

    async def test_parallel_requests_share_counts_and_queued_work_does_not_hide_output(self):
        first, second = process(), process()
        with patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', side_effect=[first, second]), \
             track_ai_activity('parallel'):
            def request():
                return asyncio.create_task(request_ai_text(instructions='synthetic', payload={},
                    config=CONFIG, mode='correction', max_output_tokens=100))
            a = request()
            await asyncio.sleep(.01)
            event(first, {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': '甲乙'}})
            await asyncio.sleep(.01)
            b = request()
            try:
                await asyncio.sleep(.01)
                state = task_ai_activity('parallel')
                self.assertEqual(state['phase'], 'receiving')
                self.assertEqual(state['active_requests'], 2)
                for child, text in [(first, '甲乙'), (second, '丙丁戊')]:
                    event(child, {'event': 'result', 'result': {'status': 'SUCCESS', 'response': text}})
                    child.stdout.feed_eof()
                await asyncio.gather(a, b)
                self.assertEqual(task_ai_activity('parallel')['output_chars'], 5)
            finally:
                a.cancel(); b.cancel()
                await asyncio.gather(a, b, return_exceptions=True)

    async def test_early_stdin_close_preserves_provider_error(self):
        child = process()
        child.stdin.drain = AsyncMock(side_effect=BrokenPipeError)
        event(child, {'event': 'result', 'result': {'status': 'FAILED', 'error': 'not logged in'}})
        child.stdout.feed_eof()
        with patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', return_value=child):
            with self.assertRaises(AIRequestError) as caught:
                await request_text('synthetic', {}, PROVIDER)
        self.assertEqual(caught.exception.kind, 'provider_auth')
        child.kill.assert_not_called()

    async def test_reader_failure_reaps_process_and_unknown_events_do_not_claim_output(self):
        child = process()
        child.stdout.feed_data(b'diagnostic\n[]\n{"event":"unknown","text":"secret"}\n')
        child.stdout.set_exception(ValueError('stream read failed'))
        with patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', return_value=child), \
             track_ai_activity('failure'), track_ai_request():
            with self.assertRaisesRegex(ValueError, 'stream read failed'):
                await request_text('synthetic', {}, PROVIDER)
            self.assertEqual(task_ai_activity('failure')['output_chars'], 0)
        child.kill.assert_called_once()
        child.wait.assert_awaited_once()

    async def test_task_feed_separates_old_error_from_new_request_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw), status='completed')
            record.segments = [{'id': 1, 'raw': '课堂原文', 'ai_error': '上次没有返回文本', 'start': 0, 'end': 10}]
            record.save_all()
            store = store_for(record)
            entered, finish = asyncio.Event(), asyncio.Event()
            async def runner():
                entered.set()
                await finish.wait()
                record.segments[0].update(corrected='已校对的课堂原文', ai_error='')
                record.save_all()
            task_id = f'refinement:{record.id}'
            with patch.object(app, 'recording_store', store):
                self.assertTrue(app._start_aux_ai_job(record, 'refinement', runner))
                task = app.background_ai_tasks[task_id]
                try:
                    await entered.wait()
                    item = next(t for t in app.compact_ai_tasks() if t['type'] == 'refinement')
                    self.assertEqual(item['error'], '')
                    self.assertEqual(item['previous_error'], '上次没有返回文本')
                    self.assertEqual(item['ai_activity']['phase'], 'waiting')
                    finish.set()
                    await task
                    self.assertIsNone(task_ai_activity(task_id))
                    self.assertFalse(any(t['id'] == task_id for t in app.compact_ai_tasks()))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


REGION = 'AGY 资料会话被 Google 拒绝：当前网络出口地区不受支持（User location is not supported for the API use）'


class TaskFailureSeparationTests(unittest.IsolatedAsyncioTestCase):
    def record(self, raw, **metadata):
        record = record_at(Path(raw), status='completed')
        record.segments = [{'id': 1, 'raw': '课堂原文', 'start': 0, 'end': 10}]
        record.metadata.update(metadata)
        record.save_all()
        return record

    def feed(self, record, kind):
        record.save_all()
        with patch.object(app, 'recording_store', store_for(record)):
            matches = [item for item in app.compact_ai_tasks() if item['type'] == kind]
        self.assertEqual(len(matches), 1)
        return matches[0]

    async def live(self, record, kind='refinement'):
        task = asyncio.create_task(asyncio.Event().wait())
        app.background_ai_tasks[f'{kind}:{record.id}'] = task
        self.addAsyncCleanup(self.stop, record, kind, task)

    async def stop(self, record, kind, task):
        app.background_ai_tasks.pop(f'{kind}:{record.id}', None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_running_retry_keeps_every_stale_source_as_previous_error_only(self):
        with tempfile.TemporaryDirectory() as raw:
            record = self.record(raw)
            record.metadata['ai_jobs'] = {'refinement': {
                'status': 'running', 'error': '作业地区拒绝', 'error_kind': 'provider_region'}}
            record.segments[0]['ai_error'] = '段落地区拒绝'
            record.metadata['course_stream'] = {'correction_error': '流式地区拒绝'}
            await self.live(record)
            item = self.feed(record, 'refinement')
            self.assertEqual(item['status'], 'running')
            self.assertEqual(item['error'], '')
            self.assertEqual(item['error_kind'], '')
            self.assertIn('作业地区拒绝', item['previous_error'])
            self.assertIn('段落地区拒绝', item['previous_error'])
            self.assertIn('流式地区拒绝', item['previous_error'])

            record.metadata['ai_jobs']['refinement']['error'] = REGION
            record.metadata['ai_jobs']['refinement']['error_kind'] = 'provider_region'
            record.segments[0]['ai_error'] = REGION
            record.metadata['course_stream'] = {'correction_error': REGION}
            item = self.feed(record, 'refinement')
            self.assertEqual(item['error'], '')
            self.assertEqual(item['error_kind'], '')
            self.assertEqual(item['previous_error'], REGION)

    async def test_each_single_source_is_previous_during_retry_and_current_when_it_fails_again(self):
        with tempfile.TemporaryDirectory() as raw:
            record = self.record(raw)
            await self.live(record)
            cases = (
                ('job.error', {'ai_jobs': {'refinement': {'status': 'running', 'error': '作业失败', 'error_kind': 'provider_region'}}}, '作业失败'),
                ('paragraph', {'segments_error': '段落失败'}, '段落失败'),
                ('stream', {'course_stream': {'correction_error': '流式失败'}}, '流式失败'),
            )
            for name, update, text in cases:
                record.metadata['ai_jobs'] = {'refinement': {'status': 'running', 'error': '', 'error_kind': 'provider_region'}}
                record.segments[0]['ai_error'] = ''
                record.metadata['course_stream'] = {}
                if 'segments_error' in update:
                    record.segments[0]['ai_error'] = update['segments_error']
                else:
                    record.metadata.update(update)
                item = self.feed(record, 'refinement')
                self.assertEqual(item['error'], '', name)
                self.assertEqual(item['error_kind'], '', name)
                self.assertEqual(item['previous_error'], text, name)
            record.metadata['ai_jobs'] = {'refinement': {
                'status': 'failed', 'error': '第二次实际失败', 'error_kind': 'network_transport'}}
            record.segments[0]['ai_error'] = '更早的段落失败'
            record.metadata['course_stream'] = {'correction_error': '更早的流式失败'}
            item = self.feed(record, 'refinement')
            self.assertEqual(item['status'], 'failed')
            self.assertEqual(item['error'], '第二次实际失败')
            self.assertEqual(item['error_kind'], 'network_transport')
            self.assertNotIn('第二次实际失败', item['previous_error'])

    async def test_success_clears_stale_stream_failure_and_control_states_stay_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            record = self.record(raw)
            record.metadata['ai_jobs'] = {'refinement': {'status': 'completed', 'error': '', 'error_kind': ''}}
            record.metadata['course_stream'] = {'correction_error': REGION}
            item = self.feed(record, 'refinement')
            self.assertEqual(item['status'], 'pending')
            self.assertEqual(item['error'], '')
            self.assertEqual(item['previous_error'], '')
            self.assertNotIn(REGION, json.dumps(item, ensure_ascii=False))
            await self.live(record)
            for status, message, kind in (
                ('paused', '任务已暂停；已完成内容保留，可继续', 'paused'),
                ('cancelled', '任务已取消；已完成内容仍然保留', 'cancelled'),
                ('interrupted', '任务已中断；已完成内容保留，可继续', 'service_interrupted'),
            ):
                record.metadata['ai_jobs'] = {'refinement': {'status': status, 'error': message, 'error_kind': kind}}
                record.segments[0]['ai_error'] = REGION
                item = self.feed(record, 'refinement')
                self.assertEqual(item['status'], status)
                self.assertEqual(item['error'], message)
                self.assertEqual(item['error_kind'], kind)
                self.assertNotEqual(item['error'], REGION)

    async def test_waiting_retry_keeps_its_current_reason(self):
        with tempfile.TemporaryDirectory() as raw:
            record = self.record(raw)
            waiting = '网络出口地区被拒，3 分钟后自动重试（第 1 次）；已完成的步骤会保留'
            record.metadata['ai_jobs'] = {'refinement': {
                'status': 'waiting_retry', 'error': waiting, 'error_kind': 'provider_region', 'retry_in_seconds': 180}}
            record.segments[0]['ai_error'] = REGION
            await self.live(record)
            item = self.feed(record, 'refinement')
            self.assertEqual(item['status'], 'waiting_retry')
            self.assertEqual(item['error'], waiting)
            self.assertEqual(item['error_kind'], 'provider_region')
            self.assertEqual(item['previous_error'], REGION)

    async def test_live_summary_and_summary_follow_the_same_attempt_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            record = self.record(raw)
            record.metadata['ai_summaries'] = [{'start': 0, 'end': 60, 'ai_error': REGION, 'error_kind': 'provider_region'}]
            record.metadata['ai_jobs'] = {'live_summary': {
                'status': 'running', 'error': '作业仍记着地区拒绝', 'error_kind': 'provider_region'}}
            await self.live(record, 'live_summary')
            item = self.feed(record, 'live_summary')
            self.assertEqual(item['status'], 'running')
            self.assertEqual(item['error'], '')
            self.assertEqual(item['error_kind'], '')
            self.assertIn(REGION, item['previous_error'])
            self.assertIn('作业仍记着地区拒绝', item['previous_error'])
            record.metadata['ai_jobs']['live_summary'] = {
                'status': 'failed', 'error': '新的响应超时', 'error_kind': 'network_read_timeout'}
            record.metadata['ai_summaries'] = [{'start': 0, 'end': 60, 'ai_error': '新的响应超时', 'error_kind': 'network_read_timeout'}]
            item = self.feed(record, 'live_summary')
            self.assertEqual(item['status'], 'failed')
            self.assertEqual(item['error'], '新的响应超时')
            self.assertEqual(item['error_kind'], 'network_read_timeout')
            record.metadata['ai_jobs']['live_summary'] = {'status': 'completed', 'error': '', 'error_kind': ''}
            record.metadata['ai_summaries'] = [{'start': 0, 'end': 60, 'summary': '提纲'}]
            app.background_ai_tasks.pop(f'live_summary:{record.id}', None)
            record.save_all()
            with patch.object(app, 'recording_store', store_for(record)):
                self.assertFalse(any(item['type'] == 'live_summary' for item in app.compact_ai_tasks()))

            record.metadata['summary_job'] = {
                'status': 'running', 'error': REGION, 'error_kind': 'provider_region', 'previous_error': '更早的失败'}
            item = self.feed(record, 'summary')
            self.assertEqual(item['error'], '')
            self.assertEqual(item['error_kind'], '')
            self.assertIn(REGION, item['previous_error'])
            self.assertIn('更早的失败', item['previous_error'])
            record.metadata['summary_job'] = {
                'status': 'failed', 'error': '第二次笔记失败', 'error_kind': 'network_transport', 'previous_error': REGION}
            item = self.feed(record, 'summary')
            self.assertEqual(item['error'], '第二次笔记失败')
            self.assertEqual(item['error_kind'], 'network_transport')
            self.assertEqual(item['previous_error'], REGION)
            waiting = '网络出口地区被拒，3 分钟后自动重试（第 2 次）；已完成的步骤会保留'
            record.metadata['summary_job'] = {
                'status': 'waiting_retry', 'error': waiting, 'error_kind': 'provider_region',
                'previous_error': REGION, 'retry_in_seconds': 180}
            item = self.feed(record, 'summary')
            self.assertEqual(item['status'], 'waiting_retry')
            self.assertEqual(item['error'], waiting)
            self.assertEqual(item['error_kind'], 'provider_region')
            self.assertEqual(item['previous_error'], REGION)
            record.metadata['summary_job'] = {
                'status': 'paused', 'error': '生成已暂停，已完成内容保留，可以继续',
                'error_kind': 'paused', 'previous_error': REGION}
            item = self.feed(record, 'summary')
            self.assertEqual(item['status'], 'paused')
            self.assertEqual(item['error'], '生成已暂停，已完成内容保留，可以继续')
            self.assertEqual(item['error_kind'], 'paused')
            record.metadata['summary_job'] = {
                'status': 'interrupted', 'error': '生成已中断，已完成内容保留，可以重新生成',
                'error_kind': 'service_interrupted', 'previous_error': REGION}
            item = self.feed(record, 'summary')
            self.assertEqual(item['status'], 'interrupted')
            self.assertEqual(item['error'], '生成已中断，已完成内容保留，可以重新生成')
            self.assertEqual(item['error_kind'], 'service_interrupted')
            record.metadata['summary_job'] = {'status': 'completed', 'error': '', 'error_kind': '', 'previous_error': REGION}
            record.save_all()
            with patch.object(app, 'recording_store', store_for(record)):
                self.assertFalse(any(item['type'] == 'summary' for item in app.compact_ai_tasks()))

    async def test_active_count_remains_the_card_count_including_cancelled(self):
        with tempfile.TemporaryDirectory() as raw:
            record = self.record(raw)
            record.metadata['ai_jobs'] = {'refinement': {
                'status': 'cancelled', 'error': '任务已取消；已完成内容仍然保留', 'error_kind': 'cancelled'}}
            record.save_all()
            with patch.object(app, 'recording_store', store_for(record)):
                payload = await app.ai_tasks()
            self.assertEqual(payload['active_count'], len(payload['tasks']))
            self.assertTrue(any(item['status'] == 'cancelled' for item in payload['tasks']))
