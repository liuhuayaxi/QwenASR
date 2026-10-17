from tests import isolation
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

from qwen_asr_app.note_budget import pack_complete_items, require_budget, request_budget
from qwen_asr_app.review_notes import coverage_batches, budget_compatible_identity, summary_signature
from qwen_asr_app import note_latex_author as author
from qwen_asr_app.agy_cli import _request_arguments
from qwen_asr_app.ai import AIRequestError


def config(window=1048576, transport='agy_cli'):
    return {'ai':{'summary_provider':{'transport':transport,'model':'gemini-3.8-flash-high',
        'cli_model':'gemini-3.8-flash-high','context_window':window,'max_output_tokens':16384}}}


class NoteBudgetTests(unittest.TestCase):
    def test_revision_uses_saved_long_window_and_reports_components_when_too_small(self):
        payload={'draft':'正文'*16000,'review':'审查'*8000,'evidence':'原资料'*46000,'latex_draft':'公式'*12000}
        budget=require_budget('修订完整笔记',payload,config())
        self.assertGreater(budget['estimated_total'],256000)
        self.assertLess(budget['estimated_total'],1048576)
        self.assertEqual(budget['output_reserve'],32768)
        with self.assertRaisesRegex(ValueError,'256,000.*尚未发送'):
            require_budget('修订完整笔记',payload,config(256000))
        self.assertEqual(request_budget('',{},config(1048576,'api'))['output_reserve'],16384)

    def test_packing_accounts_for_evidence_not_just_short_source_text(self):
        items=[{'id':i,'text':'完整来源','page':'课件'*18000} for i in range(4)]
        groups=pack_complete_items(items,lambda group:{'evidence':group},'识别',config(131072),lambda group:len(group)*500)
        self.assertEqual([len(group) for group in groups],[1,1,1,1])
        self.assertEqual([item for group in groups for item in group],items)
        bigger=pack_complete_items(items,lambda group:{'evidence':group},'识别',config(),lambda group:len(group)*500)
        self.assertEqual(bigger,[items])

    def test_output_budget_still_bounds_batches_with_one_million_input_tokens(self):
        items=list(range(100))
        groups=pack_complete_items(items,lambda group:{'ids':group},'',config(),lambda group:len(group)*1000)
        self.assertEqual([len(group) for group in groups],[26,26,26,22])
        with self.assertRaisesRegex(ValueError,'单个完整'):
            pack_complete_items([1],lambda group:{'evidence':'原文'*600000},'',config(),lambda group:1000)

    def test_new_coverage_has_larger_batches_and_resume_keeps_boundaries(self):
        blocks=[{'source':'S'+str(i),'text':'完整课堂'*100} for i in range(93)]
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);payload=lambda group:{'transcript':group}
            groups=coverage_batches(folder,blocks,payload,'识别知识',config())
            self.assertEqual([len(group) for group in groups],[26,26,26,15])
            self.assertEqual(coverage_batches(folder,blocks,payload,'识别知识',config(256000)),groups)
            self.assertEqual([b for group in groups for b in group],blocks)
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'coverage-part-0.json').write_text('{}')
            groups=coverage_batches(folder,blocks,payload,'识别知识',config())
            self.assertEqual(len(groups[0]),12)
            self.assertEqual(json.loads((folder/'coverage-batch-plan.json').read_text())['policy'],'legacy-checkpoint-groups')

    def test_budget_increase_reuses_exact_task_but_model_prompt_or_source_change_does_not(self):
        inputs=['pipeline','record',{'sources':'unaltered'}]
        old=config(256000)
        identity=hashlib.sha256(json.dumps([*inputs,summary_signature(old)],sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()[:24]
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)/identity;folder.mkdir()
            (folder/'task.json').write_text(json.dumps({'generation_config':old['ai']['summary_provider']}))
            self.assertEqual(budget_compatible_identity(raw,inputs,config()),identity)
            (folder/'task.json').write_text(json.dumps({'generation_config':config()['ai']['summary_provider'],
                'identity_generation_config':old['ai']['summary_provider']}))
            self.assertEqual(budget_compatible_identity(raw,inputs,config()),identity)
            changed=config();changed['_note_summary_custom_prompt']='different request'
            self.assertNotEqual(budget_compatible_identity(raw,inputs,changed),identity)
            changed=config();changed['ai']['summary_provider']['cli_model']='different-model'
            self.assertNotEqual(budget_compatible_identity(raw,inputs,changed),identity)
            self.assertNotEqual(budget_compatible_identity(raw,[*inputs,'source changed'],config()),identity)

    def test_cli_accepts_over_900000_characters_when_token_budget_fits(self):
        provider=config()['ai']['summary_provider']
        with patch('qwen_asr_app.agy_cli.executable',return_value='/test/agy'):
            args=_request_arguments('只返回OK',{'source':'abc '*250000},provider)
            self.assertIsNotNone(args)
        provider['context_window']=256000
        with self.assertRaises(AIRequestError) as caught:
            _request_arguments('只返回OK',{'source':'abc '*250000},provider)
        self.assertEqual(caught.exception.kind,'input_too_long')
        self.assertFalse(caught.exception.retriable)

    def test_new_latex_batches_pack_more_than_ten_and_honor_legacy_plan(self):
        knowledge=[{'id':'k'+str(i),'topic':'主题','sources':[]} for i in range(86)]
        ledger={'knowledge':knowledge}
        plans,payload,_=author.plan_latex_batches('完整参考稿',config(),ledger,{},[],[],[],{},None)
        self.assertEqual([len(group) for group in plans],[43,43])
        self.assertEqual([item for group in plans for item in group],knowledge)
        self.assertEqual(payload(plans[0])['content_reference'],'完整参考稿')
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'plan.json'
            legacy={'batches':[[item['id'] for item in knowledge[i:i+10]] for i in range(0,86,10)]}
            path.write_text(json.dumps(legacy));before=path.read_bytes()
            plans,_,_=author.plan_latex_batches('完整参考稿',config(),ledger,{},[],[],[],{},None,plan_path=path)
            self.assertEqual([len(group) for group in plans],[10]*8+[6])
            self.assertEqual(path.read_bytes(),before)


class PlanJSONRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_limit_splits_complete_sources_and_resumes_children(self):
        from qwen_asr_app.review_notes import validated_plan_part
        blocks=[{'source':'S'+str(i),'text':'完整原文','start':i,'end':i+1} for i in range(1,5)]
        def proposal(ids):
            return json.dumps({'knowledge':[{'id':'k1','kind':'new_development','sources':ids}],
                               'figures':[],'omitted_sources':[],'warnings':[]})
        package={'sources':[],'associations':[],'relevant_pages':[],'history_scope':'历史范围不足'}
        payload={'transcript':blocks,'history':[],'history_scope':'历史范围不足'}
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock(side_effect=[
                    AIRequestError('output token limit',kind='output_truncated',retriable=True),
                    proposal(['S1','S2']),proposal(['S3','S4'])])) as request:
                result=await validated_plan_part('识别知识',payload,config(),{'S1','S2','S3','S4'},package,folder,0)
                self.assertEqual(request.await_count,3)
                self.assertEqual([x['source'] for call in request.await_args_list[1:] for x in call.kwargs['payload']['transcript']],
                                 ['S1','S2','S3','S4'])
            self.assertEqual([s for k in result['knowledge'] for s in k['sources']],['S1','S2','S3','S4'])
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock()) as request:
                again=await validated_plan_part('识别知识',payload,config(),{'S1','S2','S3','S4'},package,folder,0)
                self.assertEqual(again,result)
                request.assert_not_awaited()

    async def test_malformed_json_is_preserved_and_repaired_with_complete_input(self):
        from qwen_asr_app.review_notes import validated_plan_part
        bad='{"knowledge": [{"id" "k1"}]}'
        good={'knowledge':[{'id':'k1','kind':'new_development','sources':['S1']}]}
        original={'transcript':[{'source':'S1','text':'完整课堂原文'}]}
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock(side_effect=[bad,json.dumps(good)])) as request:
                result=await validated_plan_part('知识清单',original,config(),{'S1'},{'sources':[]},folder,0)
            self.assertEqual(result['knowledge'],good['knowledge'])
            self.assertEqual(request.await_count,2)
            self.assertEqual((folder/'coverage-response-0-attempt-0.txt').read_text(),bad)
            repair=request.await_args_list[1].kwargs['payload']
            self.assertEqual(repair['source_input'],original)
            self.assertEqual(repair['invalid_proposal']['unparsed_json'],bad)

    async def test_three_malformed_returns_exhaust_budget_even_after_resume(self):
        from qwen_asr_app.review_notes import validated_plan_part
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock(return_value='{"broken":')) as request:
                with self.assertRaisesRegex(ValueError,'仍未通过校验'):
                    await validated_plan_part('知识清单',{},config(),set(),{'sources':[]},folder,0)
                with self.assertRaisesRegex(ValueError,'自动修复上限'):
                    await validated_plan_part('知识清单',{},config(),set(),{'sources':[]},folder,0)
                self.assertEqual(request.await_count,3)
                self.assertEqual(len(list(folder.glob('coverage-response-*.txt'))),3)

    async def test_explicit_retry_grants_one_bounded_round_without_overwriting_history(self):
        from qwen_asr_app.review_notes import validated_plan_part
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock(return_value='{"broken":')) as request:
                with self.assertRaises(ValueError):
                    await validated_plan_part('知识清单',{},config(),set(),{'sources':[]},folder,0)
                first={p.name:p.read_bytes() for p in folder.glob('coverage-response-*.txt')}
                retry={**config(),'_note_validation_retry_round':'explicit-user-retry-1'}
                with self.assertRaises(ValueError):
                    await validated_plan_part('知识清单',{},retry,set(),{'sources':[]},folder,0)
                with self.assertRaisesRegex(ValueError,'自动修复上限'):
                    await validated_plan_part('知识清单',{},retry,set(),{'sources':[]},folder,0)
                self.assertEqual(request.await_count,6)
                self.assertEqual(len(list(folder.glob('coverage-response-*.txt'))),6)
                self.assertEqual({name:(folder/name).read_bytes() for name in first},first)


if __name__=='__main__':unittest.main()
