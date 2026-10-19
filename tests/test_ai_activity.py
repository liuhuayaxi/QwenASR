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
