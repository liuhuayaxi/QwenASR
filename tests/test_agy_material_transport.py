"""An early CLI exit must keep the cause and never approve unseen pages."""
from tests import isolation  # noqa: F401
from tests.agy_fixtures import available_material_sandbox

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from tests.agy_fixtures import available_material_sandbox

from qwen_asr_app.agy_cli import _error_kind
from qwen_asr_app.agy_materials import MaterialSessionError, communicate, request_materials, retry_materials, sandbox_profile, parse_json


PROVIDER={'transport':'agy_cli','context_window':256000,'cli_model':'gemini-3.8-flash-high'}


def child(events=(), *, stderr=b'', exit_code=0):
    stdout=asyncio.StreamReader()
    for event in events:
        stdout.feed_data((json.dumps(event)+'\n').encode())
    stdout.feed_eof()
    errors=asyncio.StreamReader();errors.feed_data(stderr);errors.feed_eof()
    process=SimpleNamespace(stdout=stdout,stderr=errors,returncode=None,
        stdin=SimpleNamespace(write=Mock(),drain=AsyncMock(),close=Mock()))
    def finish():process.returncode=exit_code
    process.wait=AsyncMock(side_effect=finish)
    process.kill=Mock();process.terminate=Mock()
    return process


class MaterialTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(available_material_sandbox())

    async def test_model_start_failure_retries_without_switching_model_or_approving_images(self):
        failure='model gemini-3.8-flash-high is not recognized as a known model or custom model in settings'
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            success=child([{'event':'init','conversation_id':'new-session'},
                {'event':'result','result':{'status':'SUCCESS','response':'{"images":[]}'}}])
            with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
                 patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',side_effect=[child(stderr=failure.encode(),exit_code=1),success]) as run, \
                 patch('qwen_asr_app.agy_cli.asyncio.sleep',new=AsyncMock()):
                text,receipt=await request_materials('synthetic',folder,PROVIDER)
            self.assertEqual(text,'{"images":[]}');self.assertEqual(receipt['session_id'],'new-session')
            self.assertEqual(run.await_count,2)
            for call in run.call_args_list:
                self.assertEqual(call.args[call.args.index('--model')+1],PROVIDER['cli_model'])
            saved=json.loads(next((folder/'sessions').glob('failed-*.json')).read_text())
            self.assertEqual(saved['error_kind'],'agy_model_unavailable');self.assertEqual(saved['viewed_files'],[])

    async def test_figure_batch_retry_is_bounded_and_preserves_completed_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);checkpoint=folder/'figure-reading-previous.json';checkpoint.write_text('previous success')
            transient=lambda:MaterialSessionError('temporary',kind='network_transport',retriable=True)
            with patch('qwen_asr_app.agy_materials.request_materials',side_effect=[transient(),('complete',{'session_id':'new'})]) as call, \
                 patch('qwen_asr_app.agy_materials.asyncio.sleep',new=AsyncMock()):
                self.assertEqual((await retry_materials('synthetic',folder,PROVIDER,label='figure-reading'))[0],'complete')
                self.assertEqual(call.await_count,2)
            with patch('qwen_asr_app.agy_materials.request_materials',side_effect=lambda *a,**k:(_ for _ in ()).throw(transient())) as call, \
                 patch('qwen_asr_app.agy_materials.asyncio.sleep',new=AsyncMock()):
                with self.assertRaises(MaterialSessionError) as caught:await retry_materials('synthetic',folder,PROVIDER)
                self.assertEqual(call.await_count,3);self.assertFalse(caught.exception.retriable)
            self.assertEqual(checkpoint.read_text(),'previous success')
            for kind in ['provider_auth','material_tool_violation','output_truncated']:
                with patch('qwen_asr_app.agy_materials.request_materials',side_effect=MaterialSessionError('rejected',kind=kind,retriable=True)) as call:
                    with self.assertRaises(MaterialSessionError):await retry_materials('synthetic',folder,PROVIDER)
                    self.assertEqual(call.await_count,1)

    async def test_large_session_requires_every_image_to_be_actually_read(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve();images=[folder/f'page-{n:03d}.png' for n in range(25)]
            for path in images:path.write_bytes(b'synthetic-image')
            reads=[{'event':'step_update','step_update':{'state':'DONE','step_type':'tool',
                'tool_info':{'name':'view_file','parameters':{'AbsolutePath':str(path)},'output':'image/png'}}} for path in images]
            for missing in (False,True):
                events=[{'event':'init','conversation_id':'large-session'},*(reads[:-1] if missing else reads),
                    {'event':'result','result':{'status':'SUCCESS','response':'review complete'}}]
                with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
                     patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=child(events)):
                    call=request_materials('Review all images sequentially',folder,{**PROVIDER,'context_window':1048576},
                        read_paths=images,require_images=images)
                    if missing:
                        with self.assertRaisesRegex(RuntimeError,'没有实际读取全部'):await call
                    else:
                        _,receipt=await call
                        self.assertEqual(len(receipt['viewed_files']),25)

    def test_literal_json_newlines_are_preserved_but_other_invalid_syntax_is_rejected(self):
        self.assertEqual(parse_json('{"quote":"原文第一行\n第二行\t公式"}'),{'quote':'原文第一行\n第二行\t公式'})
        for value in ['{"quote":"bad\x00data"}','{"quote" "missing-colon"}','{"quote":"bad\\q escape"}']:
            with self.assertRaises(json.JSONDecodeError):parse_json(value)

    async def vision_attempt(self,folder,events,schema):
        process=child(events)
        images=[folder/'page-021.png',folder/'page-022.png']
        with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
             patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
            return await request_materials('synthetic two-page review',folder,PROVIDER,
                read_paths=images,require_images=images,schema=schema)

    def finish_recovery_fixture(self,folder):
        images=[folder/'page-021.png',folder/'page-022.png']
        for image in images:image.write_bytes(b'synthetic-page-image')
        schema={'type':'object','required':['content_findings'],'properties':{
            'content_findings':{'type':'array','items':{'type':'object','required':['page','finding'],
                'properties':{'page':{'type':'integer'},'finding':{'type':'string'}}}}}}
        init={'event':'init','conversation_id':'synthetic-finish-recovery'}
        reads=[{'event':'step_update','step_update':{'state':'DONE','step_type':'tool',
            'tool_info':{'name':'view_file','parameters':{'AbsolutePath':str(image)},'output':'image/png'}}} for image in images]
        invalid={'event':'step_update','step_update':{'state':'ERROR','step_type':'tool','tool_info':{
            'name':'finish','error':"invalid arguments:\n- at '/content_findings/0': got string, want object\n- at '/content_findings/1': got string, want object",
            'parameters':{'content_findings':['synthetic-private-response','synthetic-private-response']}}}}
        structured={'content_findings':[{'page':21,'finding':'synthetic-private-response'},
            {'page':22,'finding':'synthetic-private-response'}]}
        finish={'event':'step_update','step_update':{'state':'DONE','step_type':'tool',
            'tool_info':{'name':'finish','parameters':structured}}}
        result={'event':'result','result':{'status':'SUCCESS','structured_output':structured,'response':'ignored finish text'}}
        return schema,[init,*reads,invalid,finish,result],structured

    async def test_finish_schema_error_recovers_only_after_corrected_finish_and_structured_success(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve();schema,events,expected=self.finish_recovery_fixture(folder)
            text,receipt=await self.vision_attempt(folder,events,schema)
            self.assertEqual(json.loads(text),expected)
            self.assertEqual(receipt['recovered_finish_validation_attempts'],1)
            self.assertTrue(receipt['structured_output_used'])
            self.assertEqual(receipt['viewed_files'],[str(folder/'page-021.png'),str(folder/'page-022.png')])
            self.assertEqual(receipt['tools'],['view_file','view_file','finish'])
            saved=list((folder/'sessions').glob('*.json'));self.assertEqual(len(saved),1)
            self.assertNotIn('synthetic-private-response',saved[0].read_text())

    async def test_empty_success_replays_verified_reads_and_structured_finish(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve();schema,events,expected=self.finish_recovery_fixture(folder)
            empty=[events[0],{'event':'result','result':{'status':'SUCCESS','response':''}}]
            with patch('qwen_asr_app.agy_session.read_session',return_value={'events':events}) as restore:
                text,receipt=await self.vision_attempt(folder,empty,schema)
            self.assertEqual(json.loads(text),expected)
            self.assertEqual(len(receipt['viewed_files']),2)
            self.assertEqual(set(restore.call_args.kwargs['required_files']),
                             {str(folder/'page-021.png'),str(folder/'page-022.png')})
            self.assertEqual(restore.call_args.kwargs['schema'],schema)

    async def test_truncated_material_response_replays_only_verified_continuation(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve();schema,events,expected=self.finish_recovery_fixture(folder)
            failed=[events[0],{'event':'result','result':{'status':'FAILED','error':'exceeded the output token limit'}}]
            with patch('qwen_asr_app.agy_session.output_limit_before_failure',return_value=True), \
                 patch('qwen_asr_app.agy_session.read_session',return_value={'events':events}) as restore, \
                 patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
                 patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=child(failed,stderr=b'ordinary warning',exit_code=1)):
                text,receipt=await request_materials('synthetic',folder,PROVIDER,schema=schema,
                    read_paths=[folder/'page-021.png',folder/'page-022.png'],require_images=[folder/'page-021.png',folder/'page-022.png'])
            self.assertEqual(json.loads(text),expected)
            self.assertTrue(receipt['recovered_output_continuation'])
            self.assertTrue(restore.call_args.kwargs['require_output_continuation'])

    async def test_unrecovered_finish_error_cannot_claim_a_completed_review(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve();schema,events,_=self.finish_recovery_fixture(folder)
            for failure in ('missing-corrected-finish','no-structured-output','no-schema',
                            'new-error-after-finish','success-before-corrected-finish','last-result-failed',
                            'no-success-result','unclassified-finish-error'):
                with self.subTest(failure=failure):
                    attempt=copy.deepcopy(events);effective_schema=schema
                    if failure=='missing-corrected-finish':attempt.pop(-2)
                    elif failure=='no-structured-output':attempt[-1]['result'].pop('structured_output')
                    elif failure=='no-schema':effective_schema=None
                    elif failure=='new-error-after-finish':attempt.insert(-1,copy.deepcopy(attempt[3]))
                    elif failure=='success-before-corrected-finish':attempt[-2],attempt[-1]=attempt[-1],attempt[-2]
                    elif failure=='last-result-failed':attempt.append({'event':'result','result':{'status':'FAILED','error':'synthetic'}})
                    elif failure=='no-success-result':attempt.pop()
                    elif failure=='unclassified-finish-error':attempt[3]['step_update']['tool_info']['error']='synthetic unknown failure'
                    with self.assertRaises(RuntimeError):await self.vision_attempt(folder,attempt,effective_schema)
            self.assertFalse(list((folder/'sessions').glob('materials-*.json')))

    async def test_failed_read_or_unknown_tool_stays_fatal_even_after_valid_finish(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve();schema,events,_=self.finish_recovery_fixture(folder)
            for failure in ('read-error','read-denied-done','unknown-error-tool','unknown-done-tool','missing-image'):
                with self.subTest(failure=failure):
                    attempt=copy.deepcopy(events)
                    if failure=='read-error':attempt[1]['step_update'].update(state='ERROR')
                    elif failure=='read-denied-done':attempt[1]['step_update']['tool_info']['output']='Permission denied'
                    elif failure=='unknown-error-tool':attempt[3]['step_update']['tool_info']['name']='unknown_tool'
                    elif failure=='unknown-done-tool':attempt[-2]['step_update']['tool_info']['name']='unknown_tool'
                    elif failure=='missing-image':attempt.pop(2)
                    with self.assertRaises(RuntimeError):await self.vision_attempt(folder,attempt,schema)
            self.assertFalse(list((folder/'sessions').glob('materials-*.json')))

    def test_pipe_only_cli_failure_is_retryable(self):
        self.assertEqual(_error_kind('[Errno 32] Broken pipe'),('network_transport',True))

    async def test_interrupted_long_material_result_cannot_recover_partial_durable_text(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve()
            process=child([{'event':'init','conversation_id':'synthetic-interruption'},
                           {'event':'result','result':{'status':'FAILED','error':'interrupted'}}])
            with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
                 patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process), \
                 patch('qwen_asr_app.agy_session.read_session') as restore:
                with self.assertRaises(MaterialSessionError) as caught:
                    await request_materials('资料'*50000,folder,PROVIDER)
            restore.assert_not_called()
            self.assertEqual(caught.exception.kind,'network_transport')
            self.assertTrue(caught.exception.retriable)

    def test_terminated_material_child_is_retryable(self):
        from qwen_asr_app.agy_materials import _material_failure
        failure=_material_failure('',returncode=-15)
        self.assertEqual(failure.kind,'network_transport')
        self.assertTrue(failure.retriable)

    async def test_silent_early_exit_is_retryable_but_never_approves_pages(self):
        for code in (0,1):
            with self.subTest(exit_code=code),tempfile.TemporaryDirectory() as raw:
                process=child(exit_code=code)
                with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'),patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
                    with self.assertRaises(MaterialSessionError) as caught:
                        await request_materials('synthetic',Path(raw),PROVIDER)
                self.assertEqual(caught.exception.kind,'provider_unavailable')
                self.assertTrue(caught.exception.retriable)
                receipt=json.loads(next((Path(raw)/'sessions').glob('failed-*.json')).read_text())
                self.assertEqual(receipt['viewed_files'],[]);self.assertEqual(receipt['status'],'unreviewed')

    def test_permanent_failures_do_not_become_retryable_silent_exits(self):
        from qwen_asr_app.agy_materials import _material_failure
        for detail in ('HTTP 401 invalid_grant','quota exceeded','unknown flag --schema','sandbox_apply: denied'):
            with self.subTest(detail=detail):self.assertFalse(_material_failure(detail,returncode=1).retriable)

    def test_oauth_refresh_dns_and_connection_failures_are_transport_not_invalid_auth(self):
        exact=('API error (attempt 2): request failed: Post "https://daily-cloudcode-pa.googleapis.com/'
            'v1internal:streamGenerateContent?alt=sse": Post "https://oauth2.googleapis.com/token": '
            'dial tcp: lookup oauth2.googleapis.com: no such host')
        cases=[exact,'token refresh failed due to network error: temporary DNS issue',
            'OAuth refresh https://oauth2.googleapis.com/token: connection reset by peer',
            'OAuth refresh https://oauth2.googleapis.com/token: connection refused',
            'OAuth refresh https://oauth2.googleapis.com/token: unexpected EOF',
            'OAuth refresh https://oauth2.googleapis.com/token: i/o timeout']
        for message in cases:
            with self.subTest(message=message):self.assertEqual(_error_kind(message),('network_transport',True))
        self.assertEqual(_error_kind('OAuth refresh failed without diagnostics'),('provider_auth',False))
        self.assertEqual(_error_kind('authentication failed or timed out'),('network_connect_timeout',True))

    def test_explicit_rejected_auth_keeps_priority_over_oauth_network_or_ambiguous_timeout(self):
        for rejection in ('invalid_grant','grant revoked','HTTP 401','bad credentials','invalid credentials','invalid_client','unauthorized','not logged in'):
            for suffix in ('; token refresh failed due to network error: EOF','; OAuth https://oauth2.googleapis.com/token: connection reset','; authentication failed or timed out'):
                with self.subTest(rejection=rejection,suffix=suffix):
                    self.assertEqual(_error_kind(rejection+suffix),('provider_auth',False))

    async def test_material_refresh_network_failure_keeps_retryable_unreviewed_receipt(self):
        detail=('request failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": '
            'Post "https://oauth2.googleapis.com/token": dial tcp: lookup oauth2.googleapis.com: no such host')
        process=child([{'event':'init','conversation_id':'synthetic-oauth-refresh'},
            {'event':'result','result':{'status':'FAILED','error':detail}}])
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
             patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
            folder=Path(raw)
            with self.assertRaises(MaterialSessionError) as caught:await request_materials('synthetic',folder,PROVIDER)
            self.assertEqual(caught.exception.kind,'network_transport');self.assertTrue(caught.exception.retriable)
            saved=list((folder/'sessions').glob('failed-*.json'));self.assertEqual(len(saved),1)
            receipt=json.loads(saved[0].read_text())
            self.assertEqual(receipt['status'],'unreviewed');self.assertTrue(receipt['retriable'])
            self.assertEqual(receipt['viewed_files'],[]);self.assertEqual(receipt['error_kind'],'network_transport')
            self.assertNotIn('oauth2.googleapis.com',saved[0].read_text())

    def test_seatbelt_paths_keep_utf8_without_weakening_quote_escaping(self):
        folder=Path('/tmp/资料/通信原理 C/含"引号\\反斜线')
        profile=sandbox_profile(folder,'/bin/cat')
        self.assertIn('资料/通信原理 C',profile)
        self.assertNotIn('\\u8d44',profile)
        self.assertIn(json.dumps(str(folder.resolve()),ensure_ascii=False),profile)
        self.assertIn('(deny file-read* file-write*)',profile)
        self.assertIn('(deny process-exec)',profile)

    async def test_early_pipe_close_does_not_hide_child_diagnostics(self):
        for location in ('write','drain','close'):
            with self.subTest(location=location):
                process=child(stderr=b'sandbox-exec: sandbox_apply: Operation not permitted')
                getattr(process.stdin,location).side_effect=BrokenPipeError(32,'Broken pipe')
                stdout,stderr=await communicate(process,'synthetic message',1)
                self.assertEqual(stdout,b'')
                self.assertIn(b'sandbox_apply',stderr)
                process.wait.assert_awaited_once()

    async def test_sandbox_failure_is_actionable_and_does_not_expose_raw_output(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw).resolve()
            process=child(stderr=b'sandbox-exec: sandbox_apply: Operation not permitted\nprivate-diagnostic',exit_code=71)
            process.stdin.drain.side_effect=BrokenPipeError(32,'Broken pipe')
            with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
                 patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
                with self.assertRaises(MaterialSessionError) as caught:
                    await request_materials('synthetic',folder,PROVIDER)
            self.assertEqual(caught.exception.kind,'material_sandbox_unavailable')
            self.assertFalse(caught.exception.retriable)
            self.assertIn('受控资料沙箱无法启动',str(caught.exception))
            receipt=json.loads(next((folder/'sessions').glob('failed-*.json')).read_text())
            self.assertEqual(receipt['status'],'unreviewed')
            self.assertEqual(receipt['exit_code'],71)
            self.assertEqual(receipt['viewed_files'],[])
            self.assertNotIn('private-diagnostic',json.dumps(receipt)+str(caught.exception))

    async def test_nonzero_exit_keeps_structured_auth_cause(self):
        process=child([{'event':'result','result':{'status':'FAILED','error':'not logged in'}}],
            stderr=b'generic shutdown',exit_code=1)
        process.stdin.write.side_effect=ConnectionResetError()
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
             patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
            with self.assertRaises(MaterialSessionError) as caught:
                await request_materials('synthetic',Path(raw),PROVIDER)
        self.assertEqual(caught.exception.kind,'provider_auth')
        self.assertFalse(caught.exception.retriable)
        self.assertIn('登录',str(caught.exception))

    async def test_interrupted_result_is_retryable_without_approving_any_page(self):
        process=child([{'event':'result','result':{'status':'FAILED',
            'error':'The stream was interrupted. Please continue the task you were working on.'}}])
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
             patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
            with self.assertRaises(MaterialSessionError) as caught:
                await request_materials('synthetic',Path(raw),PROVIDER)
        self.assertEqual(caught.exception.kind,'network_transport')
        self.assertTrue(caught.exception.retriable)

    async def test_timeout_covers_blocked_stdin_and_reaps_child(self):
        process=child()
        async def block():await asyncio.Event().wait()
        process.stdin.drain=AsyncMock(side_effect=block)
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
             patch('qwen_asr_app.agy_materials.timeout_seconds',return_value=.02), \
             patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
            with self.assertRaises(MaterialSessionError) as caught:
                await asyncio.wait_for(request_materials('synthetic',Path(raw),PROVIDER),1)
        self.assertEqual(caught.exception.kind,'network_read_timeout')
        self.assertTrue(caught.exception.retriable)
        process.kill.assert_called_once();process.wait.assert_awaited_once()

    async def test_cancel_reaps_child_and_keeps_cancellation(self):
        process=child();entered=asyncio.Event()
        async def block():entered.set();await asyncio.Event().wait()
        process.stdin.drain=AsyncMock(side_effect=block)
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
             patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
            task=asyncio.create_task(request_materials('synthetic',Path(raw),PROVIDER))
            await asyncio.wait_for(entered.wait(), 1);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        process.terminate.assert_called_once();process.wait.assert_awaited_once()

    async def test_nonzero_success_result_cannot_claim_completed_review(self):
        process=child([{'event':'init','conversation_id':'synthetic-session'},
            {'event':'result','result':{'status':'SUCCESS','response':'passed'}}],exit_code=1)
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'), \
             patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process):
            with self.assertRaises(MaterialSessionError):
                await request_materials('synthetic',Path(raw),PROVIDER)


if __name__=='__main__':unittest.main()
