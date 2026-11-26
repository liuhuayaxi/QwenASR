from __future__ import annotations

from tests import isolation  # noqa: F401

import json
import subprocess
import asyncio
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock
from types import SimpleNamespace

from qwen_asr_app.agy_cli import _decode_output, executable, run_sync, request_text
from qwen_asr_app.ai import AIRequestError


class AGYCLITests(unittest.TestCase):
    def test_background_keychain_failure_is_local_and_private_log_is_removed(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from qwen_asr_app.agy_cli import consume_startup_auth_failure
        with TemporaryDirectory() as directory:
            log=Path(directory)/'startup.log'
            log.write_text('Failed to load stored token from keyring, falling back to file: exit status 36')
            error=consume_startup_auth_failure(log)
            self.assertEqual(error.kind,'provider_keychain_context')
            self.assertFalse(error.retriable)
            self.assertFalse(log.exists())
            log.write_text('Failed to load stored token from keyring: exit status 36\nPrint mode: silent auth succeeded')
            self.assertIsNone(consume_startup_auth_failure(log))
            self.assertFalse(log.exists())

    def test_text_failure_uses_only_this_invocations_startup_log(self):
        def launch(command,**kwargs):
            from pathlib import Path
            Path(command[command.index('--log-file')+1]).write_text(
                'private-auth-data\nFailed to load stored token from keyring: exit status 36')
            return subprocess.CompletedProcess(command,1,json.dumps({
                'event':'result','result':{'status':'FAILED','error':'authentication failed or timed out'}}),'')
        with patch('qwen_asr_app.agy_cli.executable',return_value='/test/agy'), \
                patch('qwen_asr_app.agy_cli.subprocess.run',side_effect=launch) as run:
            with self.assertRaises(AIRequestError) as caught:
                run_sync('test',{}, {'cli_model':'gemini-3.8-flash-high'})
            self.assertEqual(caught.exception.kind,'provider_keychain_context')
            self.assertFalse(caught.exception.retriable)
            self.assertNotIn('private-auth-data',str(caught.exception))
            from pathlib import Path
            self.assertFalse(Path(run.call_args.args[0][run.call_args.args[0].index('--log-file')+1]).exists())

    def test_sync_startup_and_timeout_failures_write_redacted_diagnostics(self):
        for error, kind in [(OSError('private credential'), 'agy_cli_unavailable'),
                            (subprocess.TimeoutExpired('agy', 30), 'network_read_timeout')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw, \
                 patch('qwen_asr_app.agy_cli.PRIVATE_ROOT', Path(raw)), \
                 patch('qwen_asr_app.agy_cli.executable', return_value='/test/agy'), \
                 patch('qwen_asr_app.agy_cli.subprocess.run', side_effect=error), \
                 self.assertLogs('qwen_asr_app.agy_cli', level='WARNING') as logs:
                with self.assertRaises(AIRequestError):
                    run_sync('secret prompt', {}, {'cli_model': 'gemini-3.8-flash-high'})
                files = list(Path(raw).glob('agy-cli/diagnostics/failed-*.json'))
                self.assertEqual(len(files), 1)
                self.assertEqual(json.loads(files[0].read_text())['error_kind'], kind)
                self.assertNotIn('private credential', files[0].read_text())
                self.assertIn(kind, logs.output[0])

    def test_blocked_keychain_lookup_is_local_and_never_a_network_retry(self):
        detail='OS credential manager unavailable (fork/exec /usr/bin/security: operation not permitted); falling back to file-based secret storage'
        failures=['',json.dumps({'event':'result','result':{'status':'FAILED','error':'Authentication failed or timed out'}})]
        for output in failures:
            with self.subTest(output=bool(output)),self.assertRaises(AIRequestError) as error:
                _decode_output(output,detail)
            self.assertEqual(error.exception.kind,'material_sandbox_auth')
            self.assertFalse(error.exception.retriable)
            self.assertIn('钥匙串',str(error.exception))

    def test_saved_cli_proxy_is_shared_by_text_and_materials_without_changing_parent_environment(self):
        import os
        from qwen_asr_app.agy_cli import subprocess_environment
        with patch.dict(os.environ, {'HTTPS_PROXY':'http://old.invalid:8000'}):
            env=subprocess_environment({'cli_proxy_url':'http://127.0.0.1:7897'})
            self.assertEqual(env['HTTPS_PROXY'],'http://127.0.0.1:7897')
            self.assertEqual(env['HTTP_PROXY'],env['HTTPS_PROXY'])
            self.assertEqual(os.environ['HTTPS_PROXY'],'http://old.invalid:8000')
        for proxy in ['file:///private/config','http://user:secret@localhost:7897','http://localhost','http://localhost:invalid']:
            with self.assertRaises(AIRequestError):subprocess_environment({'cli_proxy_url':proxy})

    def test_zero_timeout_waits_for_completion_without_a_wall_clock_deadline(self):
        from qwen_asr_app.agy_cli import timeout_seconds
        self.assertIsNone(timeout_seconds({'cli_timeout_seconds':0}))
        self.assertEqual(timeout_seconds({'cli_timeout_seconds':7200}),7200)
        completed=subprocess.CompletedProcess([],0,json.dumps({'event':'result','result':{'status':'SUCCESS','response':'OK'}}),'')
        with patch('qwen_asr_app.agy_cli.executable',return_value='/test/agy'),patch('qwen_asr_app.agy_cli.subprocess.run',return_value=completed) as run:
            self.assertEqual(run_sync('test',{}, {'cli_timeout_seconds':0,'cli_model':'gemini-3.8-flash-high'}), 'OK')
            self.assertIsNone(run.call_args.kwargs['timeout'])

    def test_model_discovery_retries_same_model_and_stops_after_three_starts(self):
        failure=subprocess.CompletedProcess([],1,'','model gemini-3.8-flash-high is not recognized as a known model or custom model in settings')
        success=subprocess.CompletedProcess([],0,json.dumps({'event':'result','result':{'status':'SUCCESS','response':'OK'}}),'')
        provider={'cli_model':'gemini-3.8-flash-high'}
        with patch('qwen_asr_app.agy_cli.executable',return_value='/test/agy'),patch('qwen_asr_app.agy_cli.time.sleep') as sleep:
            with patch('qwen_asr_app.agy_cli.subprocess.run',side_effect=[failure,success]) as run:
                self.assertEqual(run_sync('test',{},provider),'OK')
                self.assertEqual(run.call_count,2)
                for call in run.call_args_list:
                    command=call.args[0]
                    self.assertEqual(command[command.index('--model')+1],provider['cli_model'])
            with patch('qwen_asr_app.agy_cli.subprocess.run',return_value=failure) as run:
                with self.assertRaises(AIRequestError) as caught:run_sync('test',{},provider)
                self.assertEqual(run.call_count,3)
                self.assertEqual(caught.exception.kind,'agy_model_unavailable')
                self.assertFalse(caught.exception.retriable)
                self.assertIn('三次',str(caught.exception))

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
        with patch("qwen_asr_app.agy_cli.executable", return_value='/test/agy'), patch("qwen_asr_app.agy_cli.subprocess.run", return_value=completed) as run:
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
        with patch("qwen_asr_app.agy_cli.executable", return_value='/test/agy'), patch("qwen_asr_app.agy_cli.subprocess.run", return_value=completed) as run:
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
        with patch('qwen_asr_app.agy_cli.executable', return_value='/test/agy'), patch('qwen_asr_app.agy_cli.subprocess.run', return_value=subprocess.CompletedProcess([], -15, '', '')):
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
    async def test_background_request_preserves_keychain_cause_and_removes_log(self):
        from pathlib import Path
        output=asyncio.StreamReader()
        output.feed_data((json.dumps({'event':'result','result':{
            'status':'FAILED','error':'authentication failed or timed out'}})+'\n').encode())
        output.feed_eof()
        process=SimpleNamespace(returncode=1,stdin=SimpleNamespace(write=Mock(),drain=AsyncMock(),close=Mock()),
            stdout=output,stderr=SimpleNamespace(read=AsyncMock(return_value=b'')),wait=AsyncMock())
        logs=[]
        async def launch(*command,**kwargs):
            log=Path(command[command.index('--log-file')+1]);logs.append(log)
            log.write_text('Failed to load stored token from keyring: exit status 36')
            return process
        with patch('qwen_asr_app.agy_cli.executable',return_value='/test/agy'), \
                patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec',side_effect=launch) as call:
            with self.assertRaises(AIRequestError) as caught:
                await request_text('test',{}, {'cli_model':'gemini-3.8-flash-high'})
            self.assertEqual(caught.exception.kind,'provider_keychain_context')
            self.assertFalse(caught.exception.retriable)
            self.assertEqual(call.await_count,1)
            self.assertFalse(logs[0].exists())

    async def test_early_async_failures_write_diagnostics_and_server_log(self):
        for error, kind in [(OSError('secret'), 'agy_cli_unavailable'),
                            (asyncio.CancelledError(), 'cancelled')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw, \
                 patch('qwen_asr_app.agy_cli.PRIVATE_ROOT', Path(raw)), \
                 patch('qwen_asr_app.agy_cli.executable', return_value='/test/agy'), \
                 patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', new=AsyncMock(side_effect=error)), \
                 self.assertLogs('qwen_asr_app.agy_cli', level='WARNING') as logs:
                with self.assertRaises(type(error) if kind == 'cancelled' else AIRequestError):
                    await request_text('secret prompt', {}, {'cli_model': 'gemini-3.8-flash-high'})
                files = list(Path(raw).glob('agy-cli/diagnostics/failed-*.json'))
                self.assertEqual(len(files), 1)
                self.assertEqual(json.loads(files[0].read_text())['error_kind'], kind)
                self.assertNotIn('secret', files[0].read_text())
                self.assertIn(kind, logs.output[0])

    async def test_async_timeout_is_diagnosed_and_reaped(self):
        process = SimpleNamespace(returncode=None,
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
            stdout=SimpleNamespace(readline=AsyncMock(return_value=b'')),
            stderr=SimpleNamespace(read=AsyncMock(return_value=b'')), kill=Mock(), wait=AsyncMock())
        async def timeout(awaitable, **kwargs):
            awaitable.close()
            raise asyncio.TimeoutError()
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_cli.PRIVATE_ROOT', Path(raw)), \
             patch('qwen_asr_app.agy_cli.executable', return_value='/test/agy'), \
             patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)), \
             patch('qwen_asr_app.agy_cli.asyncio.wait_for', side_effect=timeout), \
             self.assertLogs('qwen_asr_app.agy_cli', level='WARNING'):
            with self.assertRaises(AIRequestError) as raised:
                await request_text('test', {}, {'cli_model': 'gemini-3.8-flash-high'})
            self.assertEqual(raised.exception.kind, 'network_read_timeout')
            self.assertEqual(len(list(Path(raw).glob('agy-cli/diagnostics/failed-*.json'))), 1)
            process.kill.assert_called_once()
            process.wait.assert_awaited_once()

    async def test_protocol_retry_exhaustion_and_cancellation_preserve_failure(self):
        error = AIRequestError('unauthorized read_file', kind='material_tool_violation', retriable=False)
        with patch('qwen_asr_app.agy_cli._request_text_once', new=AsyncMock(side_effect=error)) as call, \
             patch('qwen_asr_app.agy_cli.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(AIRequestError):
                await request_text('test', {}, {'cli_model': 'gemini-3.8-flash-high'})
            self.assertEqual(call.await_count, 3)
        waiting = asyncio.Event()
        async def sleep(_):
            waiting.set()
            await asyncio.Event().wait()
        with patch('qwen_asr_app.agy_cli._request_text_once', new=AsyncMock(side_effect=error)) as call, \
             patch('qwen_asr_app.agy_cli.asyncio.sleep', side_effect=sleep):
            task = asyncio.create_task(request_text('test', {}, {'cli_model': 'gemini-3.8-flash-high'}))
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(call.await_count, 1)

    async def test_legacy_read_file_protocol_violation_retries_fresh_turn_twice(self):
        from qwen_asr_app.agy_cli import request_text
        error=AIRequestError('AGY 文本会话尝试调用未授权工具：read_file',
                             kind='material_tool_violation', retriable=False)
        with patch('qwen_asr_app.agy_cli._request_text_once', new=AsyncMock(side_effect=[error, '完成'])) as call, \
             patch('qwen_asr_app.agy_cli.asyncio.sleep', new=AsyncMock()) as wait:
            result=await request_text('测试', {}, {'cli_model':'gemini-3.8-flash-high'})
        self.assertEqual(result, '完成')
        self.assertEqual(call.await_count, 2)
        wait.assert_awaited_once_with(2)

    async def test_model_retry_is_cancellable_and_does_not_retry_a_started_conversation(self):
        from qwen_asr_app.agy_cli import retry_model_start
        entered=asyncio.Event()
        async def wait(_):entered.set();await asyncio.Event().wait()
        error=AIRequestError('model pending',kind='agy_model_unavailable')
        operation=AsyncMock(side_effect=error)
        with patch('qwen_asr_app.agy_cli.asyncio.sleep',side_effect=wait):
            task=asyncio.create_task(retry_model_start(operation))
            await asyncio.wait_for(entered.wait(),1);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(operation.await_count,1)
        error.session_id='already-started'
        with self.assertRaises(AIRequestError):await retry_model_start(operation)
        self.assertEqual(operation.await_count,2)

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
        with patch('qwen_asr_app.agy_cli.executable', return_value='/test/agy'), patch('qwen_asr_app.agy_cli.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            task = asyncio.create_task(request_text('测试', {}, {'cli_model': 'gemini-3.8-flash-high'}))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
