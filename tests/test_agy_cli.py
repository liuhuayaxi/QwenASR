from __future__ import annotations

from tests import isolation  # noqa: F401

import json
import subprocess
import asyncio
import unittest
from unittest.mock import patch, AsyncMock, Mock
from types import SimpleNamespace

from qwen_asr_app.agy_cli import _decode_output, executable, run_sync, request_text
from qwen_asr_app.ai import AIRequestError


class AGYCLITests(unittest.TestCase):
    def test_gui_service_path_finds_user_local_installation(self):
        # Model the actual GUI launcher PATH rather than our login-shell PATH.
        with patch("qwen_asr_app.agy_cli.shutil.which", return_value=None), \
                patch("qwen_asr_app.agy_cli.Path.is_file", return_value=True), \
                patch("qwen_asr_app.agy_cli.os.access", return_value=True):
            resolved = executable({})
        self.assertTrue(resolved.endswith("/.local/bin/agy"))

    def test_stream_json_result_is_decoded(self):
        output = '\n'.join([
            json.dumps({"event": "init", "conversation_id": "x"}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "整理完成"}}),
        ])
        self.assertEqual(_decode_output(output, ""), "整理完成")

    def test_low_flash_omits_unsupported_effort_flag(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({
            "event": "result", "result": {"status": "SUCCESS", "response": "OK"}
        }) + "\n", "")
        with patch("qwen_asr_app.agy_cli.subprocess.run", return_value=completed) as run:
            self.assertEqual(run_sync("只输出 OK", {"text": "短文本"}, {
                "transport": "agy_cli", "cli_model": "gemini-3.8-flash-low", "cli_effort": "low",
            }), "OK")
        command = run.call_args.args[0]
        self.assertNotIn("--effort", command)
        self.assertIn("--input-format", command)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["event"], "user")
        self.assertIn(".local/bin", run.call_args.kwargs["env"]["PATH"])

    def test_high_flash_keeps_high_model_and_omits_newly_unsupported_effort_flag(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({
            "event": "result", "result": {"status": "SUCCESS", "response": "OK"}
        }) + "\n", "")
        with patch("qwen_asr_app.agy_cli.subprocess.run", return_value=completed) as run:
            run_sync("只输出 OK", {"text": "短文本"}, {
                "transport": "agy_cli", "cli_model": "gemini-3.8-flash-high", "cli_effort": "high",
            })
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--model')+1],'gemini-3.8-flash-high')
        self.assertNotIn('--effort',command)

    def test_location_error_is_not_automatically_retried(self):
        with self.assertRaises(AIRequestError) as caught:
            _decode_output('', 'FAILED_PRECONDITION: User location is not supported')
        self.assertEqual(caught.exception.kind, "provider_region")
        self.assertFalse(caught.exception.retriable)

    def test_interrupted_result_stream_can_be_retried(self):
        output = json.dumps({'event': 'result', 'result': {
            'status': 'FAILED', 'error': 'The stream was interrupted. Please continue the task you were working on.'}})
        with self.assertRaises(AIRequestError) as caught:
            _decode_output(output, '')
        self.assertEqual(caught.exception.kind, 'network_transport')
        self.assertTrue(caught.exception.retriable)

    def test_provider_network_issue_can_be_retried(self):
        output = json.dumps({'event': 'result', 'result': {
            'status': 'FAILED',
            'error': 'There was a network issue connecting to the server, please try again.',
        }})
        with self.assertRaises(AIRequestError) as caught:
            _decode_output(output, '')
        self.assertEqual(caught.exception.kind, 'network_transport')
        self.assertTrue(caught.exception.retriable)

    def test_native_interruption_variants_are_retryable_without_accepting_partial_text(self):
        for result in (
            {'status': 'FAILED', 'error': 'interrupted', 'response': '不完整正文'},
            {'status': 'INTERRUPTED', 'response': '不完整正文'},
        ):
            with self.subTest(result=result), self.assertRaises(AIRequestError) as caught:
                _decode_output(json.dumps({'event': 'result', 'result': result}), '')
            self.assertEqual(caught.exception.kind, 'network_transport')
            self.assertTrue(caught.exception.retriable)

    def test_interruption_does_not_hide_rejected_credentials(self):
        result = {'status': 'FAILED', 'error': 'interrupted'}
        with self.assertRaises(AIRequestError) as caught:
            _decode_output(json.dumps({'event': 'result', 'result': result}), '401 invalid_grant')
        self.assertEqual(caught.exception.kind, 'provider_auth')
        self.assertFalse(caught.exception.retriable)

    def test_terminated_child_without_result_remains_retryable(self):
        with patch('qwen_asr_app.agy_cli.subprocess.run', return_value=subprocess.CompletedProcess([], -15, '', '')):
            with self.assertRaises(AIRequestError) as caught:
                run_sync('test', {}, {'cli_model': 'gemini-3.8-flash-high'})
        self.assertEqual(caught.exception.kind, 'network_transport')
        self.assertTrue(caught.exception.retriable)

    def test_transport_failure_on_any_auth_endpoint_is_not_a_login_rejection(self):
        endpoints = ('https://www.googleapis.com/oauth2/v2/userinfo',
                     'https://oauth2.googleapis.com/token',
                     'https://accounts.google.com/login',
                     'https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist')
        for endpoint in endpoints:
            for detail, expected in (('EOF', 'network_transport'), ('connection reset by peer', 'network_transport'),
                                     ('dial tcp: lookup host: no such host', 'network_transport'),
                                     ('deadline exceeded', 'network_read_timeout'), ('HTTP 503', 'provider_unavailable')):
                error = f'Eligibility check failed: Get "{endpoint}": {detail}'
                with self.subTest(error=error), self.assertRaises(AIRequestError) as caught:
                    _decode_output(json.dumps({'event':'result','result':{'status':'FAILED','error':error}}), '')
                self.assertEqual(caught.exception.kind, expected)
                self.assertTrue(caught.exception.retriable)
                with self.assertRaises(AIRequestError) as rejected:
                    _decode_output(json.dumps({'event':'result','result':{'status':'FAILED','error':error+'; HTTP 401 invalid_grant'}}), '')
                self.assertEqual(rejected.exception.kind, 'provider_auth')
                self.assertFalse(rejected.exception.retriable)

    def test_ambiguous_auth_timeout_is_retried_but_explicit_login_failure_is_not(self):
        for message, kind, retriable in (
            ('authentication failed or timed out', 'network_connect_timeout', True),
            ('not logged in', 'provider_auth', False),
        ):
            output = json.dumps({'event': 'result', 'result': {'status': 'FAILED', 'error': message}})
            with self.subTest(message=message), self.assertRaises(AIRequestError) as caught:
                _decode_output(output, '')
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(caught.exception.retriable, retriable)


class AGYCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_reaps_its_cli_child(self):
        entered = asyncio.Event()
        async def readline():
            entered.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(returncode=None,
                                  stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
                                  stdout=SimpleNamespace(readline=readline),
                                  stderr=SimpleNamespace(read=AsyncMock(return_value=b'')),
                                  kill=Mock(), wait=AsyncMock())
        with patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            task = asyncio.create_task(request_text('测试', {}, {'cli_model': 'gemini-3.8-flash-high'}))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

