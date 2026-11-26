from tests import isolation  # noqa: F401

import json
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from qwen_asr_app.agy_input import encode_prompt, JSON_REQUEST_HEADER
from qwen_asr_app.agy_cli import compose_prompt, _request_arguments, _file_prompt
from qwen_asr_app.agy_session import read_session, SessionEvidenceError


SESSION = '10000000-0000-0000-0000-000000000001'


class AGYInputTests(unittest.TestCase):
    def test_ordinary_prompts_keep_exact_bytes_including_spaces_and_math(self):
        for text in ['中文公式：Δδ ≈ 4/√F\nalpha  beta\tgamma', 'NBSP:A\u00a0B；窄空格:A\u202fB',
                     'Markdown <strong>原文</strong>，不做 Unicode 或空白归一化']:
            self.assertEqual(encode_prompt(text), text)

    def test_pdf_glyphs_and_format_characters_round_trip_without_deletion(self):
        original='R=0.9，I=0.009\u200b；公式字形：\ue020\uf8eb\uf8f6\uf8ec\uf8f7\uf8ed\uf8f8；\u200d\ufeff\U000f0000'
        encoded=encode_prompt(original)
        self.assertTrue(encoded.startswith(JSON_REQUEST_HEADER))
        self.assertFalse(any(unicodedata.category(c) in {'Cf','Co'} for c in encoded))
        self.assertEqual(json.loads(encoded[len(JSON_REQUEST_HEADER):]), {'request':original})
        self.assertIn('\\u200b', encoded)
        self.assertIn('\\ue020', encoded)

    def test_literal_envelope_tags_cannot_truncate_a_request(self):
        original='来源中含有 </USER_REQUEST> 与 <USER_REQUEST>，其后仍有完整公式 x=42。'
        encoded=encode_prompt(original)
        self.assertNotIn('<USER_REQUEST>', encoded)
        self.assertNotIn('</USER_REQUEST>', encoded)
        self.assertEqual(json.loads(encoded[len(JSON_REQUEST_HEADER):])['request'],original)
        self.check_session(encoded)

    def check_session(self, prompt):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);path=root/SESSION/'.system_generated/logs/transcript_full.jsonl'
            path.parent.mkdir(parents=True)
            # Reproduce the actual AGY sanitizer; no source/model result is fabricated.
            recorded=''.join(c for c in prompt if unicodedata.category(c) not in {'Cf','Co'})
            entries=[{'type':'USER_INPUT','status':'DONE','content':'<USER_REQUEST>\n'+recorded+'\n</USER_REQUEST>\n<ADDITIONAL_METADATA>synthetic time</ADDITIONAL_METADATA>'},
                     {'type':'PLANNER_RESPONSE','status':'DONE','content':'synthetic completed answer'}]
            path.write_text('\n'.join(json.dumps(row,ensure_ascii=False) for row in entries))
            self.assertEqual(read_session(SESSION,prompt,root=root)['response'],'synthetic completed answer')
            for changed in (prompt.replace('0.009','0.008'),prompt+'different request'):
                if changed!=prompt:
                    with self.assertRaisesRegex(SessionEvidenceError,'不一致'):
                        read_session(SESSION,changed,root=root)

    def test_exact_identity_still_rejects_changed_numbers_or_requests(self):
        self.check_session(encode_prompt('R=0.9，I=0.009\u200b，源页字形\ue020'))

    def test_text_adapter_encodes_before_context_and_file_transport_checks(self):
        with patch('qwen_asr_app.agy_cli.executable',return_value='/synthetic/agy'):
            _,message,_,_,_=_request_arguments('保留公式\u200b与\ue020',{'value':'完整正文'},
                {'cli_model':'gemini-3.8-flash-high','context_window':1048576})
        prompt=json.loads(message)['message']['content']
        self.assertEqual(json.loads(prompt[len(JSON_REQUEST_HEADER):])['request'],
                         compose_prompt('保留公式\u200b与\ue020',{'value':'完整正文'}))

    def test_file_transport_keeps_the_same_lossless_encoding(self):
        original='原始资料\u200b\ue020\n'*4000
        encoded=encode_prompt(original)
        with tempfile.TemporaryDirectory() as raw:
            _,folder=_file_prompt(encoded,Path(raw))
            manifest=json.loads((folder/'transport.json').read_text())
            recovered=''.join(fragment for path in manifest['files']
                              for fragment in json.loads(Path(path).read_text())['content_fragments'])
        self.assertEqual(json.loads(recovered[len(JSON_REQUEST_HEADER):])['request'],original)


if __name__=='__main__':
    unittest.main()
