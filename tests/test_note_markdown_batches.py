from tests import isolation  # noqa: F401

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from qwen_asr_app.agy_cli import last_session_id
from qwen_asr_app.ai import AIRequestError
from qwen_asr_app.note_markdown_batches import write_batches


class MarkdownBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_evidence_and_all_knowledge_survive_output_split_and_resume(self):
        knowledge=[{'id':'k'+str(n),'topic':'主题','sources':['S1']} for n in range(4)]
        payload={'coverage':{'knowledge':knowledge},'images':[],
                 'transcript':[{'source':'S1','text':'完整原始推导和例题'}]}
        calls=[]
        async def request(**kwargs):
            local=kwargs['payload'];calls.append(local)
            if len(calls)==1:raise AIRequestError('output limit',kind='output_truncated')
            last_session_id.set('verified-session-'+str(len(calls)))
            return '\n'.join('<!-- knowledge:'+i+' -->完整来源讲解<!-- /knowledge:'+i+' -->' for i in local['required_knowledge_ids'])
        with tempfile.TemporaryDirectory() as raw:
            with patch('qwen_asr_app.note_markdown_batches.request_ai_text',side_effect=request):
                body,sessions=await write_batches('写作要求',payload,{},Path(raw))
            for item in knowledge:self.assertEqual(body.count('<!-- knowledge:'+item['id']+' -->'),1)
            self.assertEqual(len(sessions),3)
            self.assertTrue(all(call['transcript']==payload['transcript'] for call in calls))
            with patch('qwen_asr_app.note_markdown_batches.request_ai_text',AsyncMock()) as request:
                restored,restored_sessions=await write_batches('写作要求',payload,{},Path(raw))
                request.assert_not_awaited()
            self.assertEqual(body,restored);self.assertEqual(sessions,restored_sessions)
            changed={**payload,'transcript':[{'source':'S1','text':'changed evidence'}]}
            with self.assertRaisesRegex(ValueError,'证据不同'):
                await write_batches('写作要求',changed,{},Path(raw))


if __name__=='__main__':unittest.main()
