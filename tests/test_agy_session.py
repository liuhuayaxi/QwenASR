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
