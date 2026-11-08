from tests import isolation  # noqa: F401

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qwen_asr_app.agy_session import read_session, SessionEvidenceError, output_limit_before_failure
from qwen_asr_app.agy_cli import _file_prompt, FILE_INPUT_BYTES, _finish_output
from qwen_asr_app.ai import AIRequestError


SESSION = '10000000-0000-0000-0000-000000000001'
PROMPT = '唯一请求标记：request-93ab'


class SessionRecoveryTests(unittest.TestCase):
    def test_native_read_file_verifies_path_full_line_content_and_hash(self):
        source = self.root / 'input.json'
        source.write_text('{\n  "data": "完整来源"\n}\n')
        files = {str(source): {'lines': 3, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}}
        read = self.answer(content='', tool_calls=[{'name': 'read_file', 'args': {'Path': str(source)}}])
        output = 'Created At: synthetic\nShowing lines 1-3 of 3\n1: {\n2:   "data": "完整来源"\n3: }\n'
        receipt = {'type': 'GENERIC', 'status': 'DONE', 'content': output}
        self.save([self.user(), read, receipt, self.answer()])
        recovered = self.recover(allowed_files=files)
        self.assertEqual(recovered['viewed_files'], [str(source.resolve())])
        self.assertEqual(recovered['tools'], ['read_file'])
        for bad in [output.replace('1-3 of 3', '1-2 of 3'), output.replace('完整来源', '替换内容'),
                    output.replace('3: }', ''), output.replace('1-3 of 3', '1-3 of 4')]:
            self.save([self.user(), read, {**receipt, 'content': bad}, self.answer()])
            with self.assertRaises(SessionEvidenceError):
                self.recover(allowed_files=files)
        for args in [{'Path': str(self.root / 'outside')}, {'Path': str(source), 'Command': 'ignored'},
                     {'Path': 'input.json'}]:
            self.save([self.user(), self.answer(content='', tool_calls=[{'name': 'read_file', 'args': args}]), receipt, self.answer()])
            with self.assertRaises(SessionEvidenceError):
                self.recover(allowed_files=files)

    def test_native_read_file_image_requires_completed_media_receipt(self):
        source = self.root / 'image.png'
        source.write_bytes(b'PNG-fixture')
        files = {str(source): {'lines': 1, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}}
        read = self.answer(content='', tool_calls=[{'name': 'read_file', 'args': {'Path': str(source)}}])
        receipt = {'type': 'GENERIC', 'status': 'DONE', 'content': 'image read', 'media': [{'uri': 'fixture-image'}]}
        self.save([self.user(), read, receipt, self.answer()])
        self.assertEqual(self.recover(allowed_files=files)['viewed_files'], [str(source.resolve())])
        receipt.pop('media')
        self.save([self.user(), read, receipt, self.answer()])
        with self.assertRaisesRegex(SessionEvidenceError, '图片回执'):
            self.recover(allowed_files=files)

    def test_redundant_past_end_read_requires_an_explicit_complete_read_first(self):
        source=self.root/'input-0000.json';source.write_text('{"source":"complete"}')
        files={str(source):{'lines':1,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}
        read=self.answer(content='',tool_calls=[{'name':'view_file','args':{'AbsolutePath':str(source)}}])
        done={'type':'GENERIC','status':'DONE','content':'The above content shows the entire, complete file contents of the requested file.'}
        extra=self.answer(content='',tool_calls=[{'name':'view_file','args':{'AbsolutePath':str(source),'ContentOffset':15000}}])
        failure={'type':'GENERIC','status':'ERROR','content':'ContentOffset 15000 exceeds line range size 14922'}
        entries=[self.user(),read,done,extra,failure,self.answer()]
        self.save(entries)
        result=self.recover(allowed_files=files)
        self.assertEqual(result['response'],'完整最终答案');self.assertEqual(result['redundant_read_errors'],1)
        self.assertEqual(len([e for e in result['events'] if e.get('event')=='step_update']),1)
        variants=[
            [self.user(),extra,failure,self.answer()],
            [self.user(),read,{**done,'content':'partial file'},extra,failure,self.answer()],
            [self.user(),read,done,extra,{**failure,'content':'permission denied'},self.answer()],
            [self.user(),read,done,extra,{**failure,'status':'CANCELLED'},self.answer()],
        ]
        for variant in variants:
            self.save(variant)
            with self.assertRaises(SessionEvidenceError):self.recover(allowed_files=files)
        self.save(entries);source.write_text('source edited during review')
        with self.assertRaisesRegex(SessionEvidenceError,'改变'):self.recover(allowed_files=files)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root/SESSION/'.system_generated/logs/transcript_full.jsonl'
        self.path.parent.mkdir(parents=True)

    def save(self, entries):
        self.path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in entries))

    def user(self, text=PROMPT):
        return {'type': 'USER_INPUT', 'status': 'DONE', 'content': '<USER_REQUEST>\n'+text}

    def answer(self, **values):
        return {'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': '完整最终答案',
                'thinking': '不能当答案返回的思考', **values}

    def recover(self, **kwargs):
        return read_session(SESSION, PROMPT, root=self.root, **kwargs)

    def test_recovers_completed_answer_only_from_the_exact_turn(self):
        self.save([self.user(), self.answer()])
        self.assertEqual(self.recover()['response'], '完整最终答案')
        self.save([self.user('其他请求'), self.answer()])
        with self.assertRaisesRegex(SessionEvidenceError, '不一致'):
            self.recover()
        self.save([self.user(), self.answer(status='ACTIVE')])
        with self.assertRaisesRegex(SessionEvidenceError, '完整'):
            self.recover()
        self.save([self.user(), self.answer(content='', thinking='只有思考')])
        with self.assertRaises(SessionEvidenceError):
            self.recover()

    def test_adapter_recovers_only_successful_empty_results(self):
        self.save([self.user(),self.answer()])
        message=json.dumps({'event':'user','message':{'content':PROMPT}})
        def saved(session,prompt,**kwargs):
            return read_session(session,prompt,root=self.root,**kwargs)
        success=json.dumps({'event':'result','result':{'status':'SUCCESS','response':'','conversation_id':SESSION}})
        failure=json.dumps({'event':'result','result':{'status':'FAILED','error':'not logged in','conversation_id':SESSION}})
        with patch('qwen_asr_app.agy_session.read_session',side_effect=saved) as read:
            self.assertEqual(_finish_output(success,'',self.root,message),'完整最终答案')
            with self.assertRaises(AIRequestError):
                _finish_output(failure,'',self.root,message)
            self.assertEqual(read.call_count,1)
        with self.assertRaises(AIRequestError) as caught:
            _finish_output(failure,'harmless startup warning',self.root,message,1)
        self.assertEqual(caught.exception.kind,'provider_auth')
        nonempty=json.dumps({'event':'result','result':{'status':'SUCCESS','response':'must reject'}})
        with self.assertRaisesRegex(AIRequestError,'异常退出'):
            _finish_output(nonempty,'',self.root,message,1)

    def test_failed_or_truncated_turn_never_becomes_success(self):
        self.save([self.user(PROMPT+'\nNOTE: The output was truncated because it was too long.'), self.answer()])
        with self.assertRaisesRegex(SessionEvidenceError, '截断'):
            self.recover()
        self.save([self.user(), self.answer(), {'type':'GENERIC','status':'ERROR','content':'permission denied'}])
        with self.assertRaisesRegex(SessionEvidenceError, '失败'):
            self.recover()
        self.save([self.user(), self.answer(tool_calls=[{'name':'run_command','args':{}}])])
        with self.assertRaisesRegex(SessionEvidenceError, '未授权工具'):
            self.recover()

    def test_output_limit_is_not_hidden_by_a_later_connection_reset(self):
        self.save([self.user(),self.answer(content='incomplete JSON'),
                   {'type':'ERROR_MESSAGE','status':'DONE','content':'Your previous response was cut off because it exceeded the output token limit'}])
        self.assertTrue(output_limit_before_failure(SESSION,PROMPT,root=self.root))
        self.assertFalse(output_limit_before_failure(SESSION,'different request',root=self.root))
        (self.root/'transport.json').write_text(json.dumps({'files':{}}))
        stream=json.dumps({'event':'result','result':{'status':'FAILED','conversation_id':SESSION,'error':'connection reset by peer'}})
        message=json.dumps({'event':'user','message':{'content':PROMPT}})
        def probe(session,prompt):return output_limit_before_failure(session,prompt,root=self.root)
        with patch('qwen_asr_app.agy_session.output_limit_before_failure',side_effect=probe):
            with self.assertRaises(AIRequestError) as caught:
                _finish_output(stream,'',self.root,message,1)
            self.assertEqual(caught.exception.kind,'output_truncated')
            auth=stream.replace('connection reset by peer','not logged in')
            with self.assertRaises(AIRequestError) as caught:
                _finish_output(auth,'',self.root,message,1)
            self.assertEqual(caught.exception.kind,'provider_auth')

    def test_failed_cli_recovers_only_a_verified_complete_output_continuation(self):
        source=self.root/'input.json';source.write_text('complete source')
        files={str(source):{'lines':1,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}
        (self.root/'transport.json').write_text(json.dumps({'files':files}))
        read=self.answer(content='',tool_calls=[{'name':'view_file','args':{'AbsolutePath':str(source)}}])
        done={'type':'GENERIC','status':'DONE','content':'source read'}
        limit={'type':'ERROR_MESSAGE','status':'DONE','content':'Error: Your previous response was cut off because it exceeded the output token limit\nRetries remaining: 3'}
        answer='{"answer":"完整最终答案"}'
        valid=[self.user(),read,done,self.answer(content='partial'),limit,self.answer(content=answer)]
        self.save(valid)
        stream=json.dumps({'event':'result','result':{'status':'FAILED','conversation_id':SESSION,'error':limit['content']}})
        message=json.dumps({'message':{'content':PROMPT}})
        def saved(session,prompt,**kwargs):return read_session(session,prompt,root=self.root,**kwargs)
        def probe(session,prompt):return output_limit_before_failure(session,prompt,root=self.root)
        with patch('qwen_asr_app.agy_session.read_session',side_effect=saved),patch('qwen_asr_app.agy_session.output_limit_before_failure',side_effect=probe):
            for code in (0,1):
                for stderr in ('','harmless startup warning','discovery: permission denied for unrelated file',
                               'not logged in\nPrint mode: silent auth succeeded'):
                    self.assertEqual(_finish_output(stream,stderr,self.root,message,code),answer)
            for broken in [valid[:-1],valid+[{'type':'ERROR_MESSAGE','status':'DONE','content':'not logged in'}],valid[:1]+valid[3:]]:
                self.save(broken)
                with self.assertRaises(AIRequestError):_finish_output(stream,'',self.root,message,1)
            self.save(valid)
            with self.assertRaises(AIRequestError):_finish_output(stream,'HTTP 401 invalid_grant',self.root,message,1)
            with self.assertRaises(AIRequestError):_finish_output(stream,'Print mode: silent auth succeeded\nHTTP 401 invalid_grant',self.root,message,0)
            source.write_text('changed')
            with self.assertRaises(AIRequestError):_finish_output(stream,'',self.root,message,1)

    def test_json_suffixes_are_joined_verbatim_only_across_explicit_limits(self):
        limit={'type':'ERROR_MESSAGE','status':'DONE','content':'Your previous response exceeded the output token limit'}
        first=self.answer(content='{"knowledge":[{"quote":"原文 ')
        middle=self.answer(content='有空格"}],"coverage":[')
        last=self.answer(content='1,2]}')
        entries=[self.user(),first,limit,middle,dict(limit),last]
        self.save(entries)
        recovered=self.recover(require_output_continuation=True)
        self.assertEqual(recovered['response'],first['content']+middle['content']+last['content'])
        self.assertEqual(recovered['output_continuation_fragments'],3)
        self.assertEqual(json.loads(recovered['response'])['knowledge'][0]['quote'],'原文 有空格')
        for broken in (
            entries[:-1],
            entries[:3]+[self.answer(content='extra prose')]+entries[3:],
            entries[:-1]+[self.answer(content='broken suffix')],
            [self.user(),self.answer(content='{"a":1,'),limit,self.answer(content='"a":2}')],
        ):
            self.save(broken)
            with self.assertRaises(SessionEvidenceError):self.recover(require_output_continuation=True)

    def test_successful_stream_suffix_still_uses_full_verified_continuation(self):
        first='{"decisions":[';last='{"id":1}]}'
        self.save([self.user(),self.answer(content=first),
            {'type':'ERROR_MESSAGE','status':'DONE','content':'Your previous response exceeded the output token limit'},
            self.answer(content=last)])
        stream=json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':SESSION,'response':last}})
        message=json.dumps({'message':{'content':PROMPT}})
        def probe(session,prompt):return output_limit_before_failure(session,prompt,root=self.root)
        def saved(session,prompt,**kwargs):return read_session(session,prompt,root=self.root,**kwargs)
        with patch('qwen_asr_app.agy_session.output_limit_before_failure',side_effect=probe),patch('qwen_asr_app.agy_session.read_session',side_effect=saved):
            self.assertEqual(_finish_output(stream,'ordinary warning',self.root,message,0),first+last)
            self.save([self.user(),self.answer(content=first),
                {'type':'ERROR_MESSAGE','status':'DONE','content':'Your previous response exceeded the output token limit'},
                self.answer(content='still broken')])
            with self.assertRaises(AIRequestError) as caught:_finish_output(stream,'ordinary warning',self.root,message,0)
            self.assertEqual(caught.exception.kind,'output_truncated')
            self.assertTrue(caught.exception.retriable)
            diagnostic = json.loads(next((self.root/'diagnostics').glob('*.json')).read_text())
            self.assertNotIn('ordinary warning', json.dumps(diagnostic))
            self.assertTrue(set(diagnostic) <= {
                'time', 'model', 'path_category', 'continued', 'sandbox', 'error_kind',
                'exit_code', 'duration_seconds', 'hostname', 'session_id', 'retriable',
            })

    def test_failure_diagnostic_keeps_one_hostname_and_drops_the_payload(self):
        from qwen_asr_app.agy_cli import _call_facts, _diagnostic_hostname, _failure_metadata
        self.assertEqual(_diagnostic_hostname('User location is not supported for the API use.'), 'unknown')
        self.assertEqual(_diagnostic_hostname('dial tcp 10.1.2.3:443: connection reset'), 'unknown')
        self.assertEqual(_diagnostic_hostname('https://user:secret-token@daily-cloudcode-pa.googleapis.com/path?q=1'), 'unknown')
        self.assertEqual(
            _diagnostic_hostname('Post "https://a.example/x": Post "https://b.example/y"'),
            'unknown',
        )
        self.assertEqual(
            _diagnostic_hostname('Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"'),
            'daily-cloudcode-pa.googleapis.com',
        )
        stderr = (
            'ordinary warning\n'
            'Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse&key=secret-token": '
            'FAILED_PRECONDITION: User location is not supported for the API use.\n'
            '/Users/private/lecture.pdf secret-conversation-id'
        )
        stdout = json.dumps({'event': 'result', 'result': {
            'status': 'FAILED', 'error': 'User location is not supported for the API use.',
            'conversation_id': SESSION}})
        message = json.dumps({'event': 'user', 'message': {'content': PROMPT + ' private lecture text'}})
        _call_facts.set({
            'model': 'gemini-3.8-flash-high', 'path_category': 'text', 'continued': False,
            'sandbox': False, 'started': __import__('time').monotonic() - 10.2,
        })
        with self.assertRaises(AIRequestError):
            _finish_output(stdout, stderr, self.root, message, 3)
        saved = json.loads(next((self.root / 'diagnostics').glob('failed-*.json')).read_text())
        text = json.dumps(saved, ensure_ascii=False)
        self.assertEqual(saved['model'], 'gemini-3.8-flash-high')
        self.assertEqual(saved['path_category'], 'text')
        self.assertIs(saved['continued'], False)
        self.assertIs(saved['sandbox'], False)
        self.assertEqual(saved['error_kind'], 'provider_region')
        self.assertEqual(saved['exit_code'], 3)
        self.assertEqual(saved['hostname'], 'daily-cloudcode-pa.googleapis.com')
        self.assertGreaterEqual(saved['duration_seconds'], 10)
        self.assertRegex(saved['time'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertTrue(set(saved) <= set(_failure_metadata(
            model_name='m', path_category='text', continued=False, sandbox=False,
            started=None, error_kind='provider_region', exit_code=1, detail='',
        )) | {'session_id', 'retriable'})
        for hidden in ('ordinary warning', 'secret-token', 'alt=sse', 'streamGenerateContent', 'https://',
                       '/Users/private', 'lecture.pdf', 'private lecture text', PROMPT, 'secret-conversation-id',
                       'User location is not supported'):
            self.assertNotIn(hidden, text)
        ambiguous = 'Post "https://oauth2.googleapis.com/token": lookup daily-cloudcode-pa.googleapis.com'
        _call_facts.set({'model': '../secret', 'path_category': 'not-a-path', 'continued': True, 'sandbox': True, 'started': None})
        before = {path.name for path in (self.root / 'diagnostics').glob('failed-*.json')}
        with self.assertRaises(AIRequestError):
            _finish_output(stdout, ambiguous, self.root, message, 3)
        created = [path for path in (self.root / 'diagnostics').glob('failed-*.json') if path.name not in before]
        self.assertEqual(len(created), 1)
        again = json.loads(created[0].read_text())
        self.assertEqual(again['hostname'], 'unknown')
        self.assertEqual(again['model'], 'unknown')
        self.assertEqual(again['path_category'], 'unknown')
        self.assertIsNone(again['duration_seconds'])
        blob = json.dumps(again)
        self.assertNotIn('oauth2.googleapis.com', blob)
        self.assertNotIn('daily-cloudcode-pa.googleapis.com', blob)
        self.assertNotIn('../secret', blob)

    def test_request_arguments_record_model_path_and_sandbox_without_the_prompt(self):
        from qwen_asr_app.agy_cli import _call_facts, _request_arguments
        provider = {'cli_model': 'gemini-3.8-flash-high', 'context_window': 1_000_000}
        with tempfile.TemporaryDirectory() as raw, \
             patch('qwen_asr_app.agy_cli.PRIVATE_ROOT', Path(raw)), \
             patch('qwen_asr_app.agy_cli.executable', return_value='/bin/echo'):
            _request_arguments('short instruction', {'text': 'private'}, provider, conversation='secret-conversation-id')
            facts = dict(_call_facts.get())
            self.assertEqual(facts['model'], 'gemini-3.8-flash-high')
            self.assertEqual(facts['path_category'], 'text')
            self.assertIs(facts['continued'], True)
            self.assertIs(facts['sandbox'], False)
            self.assertNotIn('secret-conversation-id', json.dumps({key: value for key, value in facts.items() if key != 'started'}))
            self.assertNotIn('private', json.dumps({key: value for key, value in facts.items() if key != 'started'}))
            with patch('qwen_asr_app.agy_cli.sys.platform', 'darwin'), \
                 patch('qwen_asr_app.agy_cli.shutil.which', return_value='/usr/bin/sandbox-exec'):
                _request_arguments('甲' * 60000, {'text': 'private lecture'}, provider, conversation='')
            sandboxed = _call_facts.get()
            self.assertEqual(sandboxed['path_category'], 'text_files')
            self.assertIs(sandboxed['sandbox'], True)
            self.assertIs(sandboxed['continued'], False)
            self.assertNotIn('private lecture', json.dumps({key: value for key, value in sandboxed.items() if key != 'started'}))
            self.assertNotIn(raw, json.dumps({key: value for key, value in sandboxed.items() if key != 'started'}))

    def test_limit_probe_requires_exact_request_not_a_matching_substring(self):
        self.save([self.user('other prefix '+PROMPT),
            {'type':'ERROR_MESSAGE','status':'DONE','content':'exceeded the output token limit'}])
        self.assertFalse(output_limit_before_failure(SESSION,PROMPT,root=self.root))

    def test_all_file_lines_must_have_successful_reads_and_unchanged_bytes(self):
        source = self.root/'source.json'
        source.write_text('第一行\n第二行\n第三行\n')
        spec = {str(source): {'lines':3,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}
        def call(start, end):
            return self.answer(content='', tool_calls=[{'name':'view_file','args':{
                'AbsolutePath':json.dumps(str(source)), 'StartLine':str(start), 'EndLine':str(end)}}])
        done = {'type':'GENERIC','status':'DONE','content':'successful file read'}
        self.save([self.user(),call(1,2),done,self.answer()])
        with self.assertRaisesRegex(SessionEvidenceError, '没有完整读取'):
            self.recover(allowed_files=spec)
        self.save([self.user(),call(1,2),done,call(3,3),done,self.answer()])
        self.assertEqual(self.recover(allowed_files=spec)['viewed_files'],[str(source.resolve())])
        source.write_text('changed')
        with self.assertRaisesRegex(SessionEvidenceError, '改变'):
            self.recover(allowed_files=spec)

    def test_transport_reassembles_every_character_with_bounded_files(self):
        text = ('中文公式 \\frac{a}{b}\n"原话"😀'*10000)+'尾部结束标记'
        prompt, folder = _file_prompt(text, self.root)
        manifest = json.loads((folder/'transport.json').read_text())
        recovered = []
        for path in manifest['files']:
            data = Path(path).read_bytes()
            self.assertLess(len(data), FILE_INPUT_BYTES+200)
            recovered.extend(json.loads(data)['content_fragments'])
        self.assertEqual(''.join(recovered),text)
        self.assertLess(len(prompt.encode()),80_000)
        self.assertNotIn('尾部结束标记',prompt)
        self.assertNotIn('必须依完整任务要求实际查看指定图片',prompt)
        image=self.root/'approved-original-page.png'
        material_prompt,_=_file_prompt(text,self.root,additional_read_paths=[image])
        self.assertIn(str(image.resolve()),material_prompt)
        self.assertIn('allowed_additional_files',material_prompt)
        self.assertIn('必须依完整任务要求实际查看指定图片',material_prompt)

    def test_structured_finish_recovery_preserves_native_validation_and_image_reads(self):
        image = self.root/'page.png'
        image.write_bytes(b'page image')
        specs = {str(image):{'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'lines':1}}
        read = self.answer(content='',tool_calls=[{'name':'view_file','args':{'AbsolutePath':json.dumps(str(image))}}])
        finish = self.answer(content='',tool_calls=[{'name':'finish','args':{
            'status':'"passed"','viewed_pages':'[1]','issues':'[]','toolAction':'"Complete review"'}}])
        done = {'type':'GENERIC','status':'DONE','content':'completed'}
        invalid = {'type':'GENERIC','status':'ERROR','content':'invalid arguments: missing status'}
        reminder=self.user("You did not call any tools. You should continue calling tools until you've completed your task. If you have completed your task call the finish tool."+'\n</USER_REQUEST>')
        self.save([self.user(),read,done,reminder,finish,invalid,finish,done])
        result = self.recover(allowed_files=specs,required_files=[image],schema={'type':'object'})
        self.assertEqual(json.loads(result['response']),{'status':'passed','viewed_pages':[1],'issues':[]})
        self.assertEqual(result['events'][-2]['step_update']['state'],'DONE')
        self.save([self.user(),read,done,self.user('另一个用户任务'),finish,done])
        with self.assertRaisesRegex(SessionEvidenceError,'独立请求'):
            self.recover(allowed_files=specs,schema={'type':'object'})
        self.save([self.user(),read,invalid,finish,done])
        with self.assertRaisesRegex(SessionEvidenceError,'失败'):
            self.recover(allowed_files=specs,schema={'type':'object'})


if __name__ == '__main__':
    unittest.main()
