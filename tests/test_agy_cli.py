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

    def test_high_flash_uses_selected_effort(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({
            "event": "result", "result": {"status": "SUCCESS", "response": "OK"}
        }) + "\n", "")
        with patch("qwen_asr_app.agy_cli.subprocess.run", return_value=completed) as run:
            run_sync("只输出 OK", {"text": "短文本"}, {
                "transport": "agy_cli", "cli_model": "gemini-3.8-flash-high", "cli_effort": "high",
            })
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--effort") + 1], "high")

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


class AGYCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_reaps_its_cli_child(self):
        entered = asyncio.Event()
        async def communicate(*args):
            entered.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(returncode=None, communicate=communicate,
                                  kill=Mock(), wait=AsyncMock())
        with patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            task = asyncio.create_task(request_text('测试', {}, {'cli_model': 'gemini-3.8-flash-high'}))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

