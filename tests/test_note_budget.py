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
    def test_note_runtime_wait_policy_does_not_mutate_saved_settings_or_model(self):
        from qwen_asr_app.review_notes import note_runtime_config
        original=config();original['ai']['summary_provider']['cli_timeout_seconds']=1800
        runtime=note_runtime_config(original)
        self.assertEqual(original['ai']['summary_provider']['cli_timeout_seconds'],1800)
        self.assertEqual(runtime['ai']['summary_provider']['cli_timeout_seconds'],0)
        self.assertEqual(runtime['ai']['summary_provider']['cli_model'],original['ai']['summary_provider']['cli_model'])

    def test_history_rename_preserves_legacy_checkpoint_and_new_task_identity(self):
        from qwen_asr_app.review_notes import history_with_saved_titles
        from qwen_asr_app.review_materials import history_content_signature
        history=[{'id':'previous','date':'2026-09-03','title':'旧课名',
                  'transcript_sha256':'original','refinement_sha256':'verified'}]
        inputs=['pipeline','record',history]
        old=hashlib.sha256(json.dumps([*inputs,summary_signature(config())],sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()[:24]
        renamed=[{**history[0],'title':'自动生成的新课名'}]
        with tempfile.TemporaryDirectory() as raw:
            # New identities bind content, not mutable display labels.
            stable=budget_compatible_identity(raw,inputs,config(),history_index=2)
            self.assertEqual(stable,budget_compatible_identity(raw,['pipeline','record',renamed],config(),history_index=2))
            folder=Path(raw)/old;folder.mkdir()
            (folder/'task.json').write_text(json.dumps({'generation_config':config()['ai']['summary_provider']}))
            (folder/'proposed-note.json').write_text(json.dumps({'material_version':{'history':history}}))
            self.assertEqual(budget_compatible_identity(raw,['pipeline','record',renamed],config(),history_index=2),old)
            self.assertEqual(history_with_saved_titles(folder,renamed),history)
            self.assertEqual(history_content_signature(history),history_content_signature(renamed))
            for key in ('id','date','transcript_sha256','refinement_sha256'):
                changed=[{**renamed[0],key:'changed-source'}]
                self.assertNotEqual(history_content_signature(history),history_content_signature(changed))
                self.assertNotEqual(budget_compatible_identity(raw,['pipeline','record',changed],config(),history_index=2),old)

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
        self.assertEqual([item for group in groups for item in group],items)
        self.assertTrue(all(len(group)*1000<=16384*.8 for group in groups))
        smaller=config();smaller['ai']['summary_provider']['max_output_tokens']=8000
        small_groups=pack_complete_items(items,lambda group:{'ids':group},'',smaller,lambda group:len(group)*1000)
        self.assertGreater(len(small_groups),len(groups))
        with self.assertRaisesRegex(ValueError,'单个完整'):
            pack_complete_items([1],lambda group:{'evidence':'原文'*600000},'',config(),lambda group:1000)

    def test_new_coverage_uses_whole_context_and_resume_keeps_boundaries(self):
        blocks=[{'source':'S'+str(i),'text':'完整课堂'*100} for i in range(93)]
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);payload=lambda group:{'transcript':group}
            groups=coverage_batches(folder,blocks,payload,'识别知识',config())
            self.assertEqual(groups, [blocks])
            self.assertEqual(coverage_batches(folder,blocks,payload,'识别知识',config(256000)),groups)
            self.assertEqual([b for group in groups for b in group],blocks)
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'coverage-part-0.json').write_text('{}')
            groups=coverage_batches(folder,blocks,payload,'识别知识',config())
            self.assertEqual(len(groups[0]),12)
            self.assertEqual(json.loads((folder/'coverage-batch-plan.json').read_text())['policy'],'legacy-checkpoint-groups')

    def test_long_agy_context_uses_one_coverage_conversation_when_output_fits(self):
        blocks=[{'source':'S'+str(i),'text':'完整课堂内容 '*30} for i in range(90)]
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            groups=coverage_batches(folder,blocks,lambda group:{'transcript':group},'识别知识',config())
            self.assertEqual(len(groups),1)
            self.assertEqual(len(groups[0]),90)
            self.assertEqual(json.loads((folder/'coverage-batch-plan.json').read_text())['policy'],
                             'complete-evidence-input-and-output-v2-long-context-one-turn')

    def test_whole_coverage_uses_saved_window_including_output_and_repair_space(self):
        blocks=[{'source':'S'+str(i),'text':'完整课堂内容 '*80} for i in range(90)]
        with tempfile.TemporaryDirectory() as raw:
            # More than the former 64K target is allowed when the full
            # source, response and repair proposal fit a 1M context.
            groups=coverage_batches(Path(raw),blocks,lambda group:{'transcript':group},'知识',config())
            self.assertEqual(groups, [blocks])
        with tempfile.TemporaryDirectory() as raw:
            groups=coverage_batches(Path(raw),blocks,lambda group:{'transcript':group},'知识',config(131072))
            self.assertGreater(len(groups), 1)
            self.assertEqual([b for g in groups for b in g], blocks)
        with tempfile.TemporaryDirectory() as raw:
            groups=coverage_batches(Path(raw),blocks,lambda group:{'transcript':group},'知识',config(1048576,'api'))
            self.assertGreater(len(groups), 1)

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
        self.assertTrue(all(10<len(group)<=21 for group in plans[:-1]))
        self.assertEqual(sum(map(len,plans)),86)
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
    async def test_missing_objective_can_link_existing_knowledge_without_duplicating_content(self):
        from tests.test_review_scope import lesson_scope,plan,BLOCKS
        from qwen_asr_app.review_notes import validated_plan_part
        original=plan();previous=json.loads(json.dumps(original));previous['knowledge'][0]['objective_ids']=[]
        previous['knowledge'][0]['scope_level']='unconfirmed';previous['knowledge'][0]['formula_policy']='unconfirmed'
        previous['knowledge'][0].pop('parameter_only_objective_ids')
        package={'sources':[],'associations':[],'relevant_pages':[],'history_scope':'原历史范围',
                 'learning_scope':lesson_scope(),'_scope_transcript_blocks':BLOCKS}
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            (folder/'coverage-validation-0-attempt-0.json').write_text(json.dumps({'attempt':0,'proposal':previous,
                'validation_error':'本节涉及的掌握要求缺少知识覆盖：gain'}))
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock(return_value=json.dumps({
                    'objective_links':[{'knowledge_id':'k1','objective_ids':['gain']}],'knowledge':[]}))):
                result=await validated_plan_part('知识清单',{'transcript':BLOCKS,'history':[]},config(),{'S1'},package,folder,0)
            self.assertEqual(len(result['knowledge']),2)
            self.assertEqual(result['knowledge'][0]['objective_ids'],['gain'])
            self.assertEqual(result['knowledge'][0]['topic'],previous['knowledge'][0]['topic'])
            self.assertEqual(result['objective_coverage'],previous['objective_coverage'])

    async def test_missing_mastery_adds_only_bound_knowledge_and_keeps_valid_proposal(self):
        from tests.test_review_scope import lesson_scope,plan,BLOCKS
        from qwen_asr_app.review_notes import validated_plan_part
        complete=plan();previous={**complete,'knowledge':complete['knowledge'][1:]}
        package={'sources':[],'associations':[],'relevant_pages':[],'history_scope':'此前课次范围',
                 'learning_scope':lesson_scope(),'_scope_transcript_blocks':BLOCKS}
        payload={'transcript':BLOCKS,'history':[],'learning_scope':lesson_scope()}
        addition={**complete['knowledge'][0]};addition.pop('parameter_only_objective_ids')
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            (folder/'coverage-validation-0-attempt-0.json').write_text(json.dumps({
                'attempt':0,'max_repairs':2,'proposal':previous,
                'validation_error':'本节涉及的掌握要求缺少知识覆盖：gain'}))
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock(return_value=json.dumps({'knowledge':[addition]}))) as request:
                result=await validated_plan_part('知识清单',payload,config(),{'S1'},package,folder,0)
            self.assertEqual(request.await_count,1)
            sent=request.await_args.kwargs['payload']
            self.assertNotIn('invalid_proposal',sent)
            self.assertEqual(sent['missing_objective_ids'],['gain'])
            self.assertEqual([item['id'] for item in sent['source_input']['learning_scope']['items']],['gain'])
            self.assertEqual(result['knowledge'][0],previous['knowledge'][0])
            self.assertEqual(result['objective_coverage'],previous['objective_coverage'])
            self.assertEqual(result['knowledge'][-1]['id'],'repair-0-1-0')

    async def test_invalid_targeted_patch_keeps_original_proposal_for_next_attempt(self):
        from tests.test_review_scope import lesson_scope,plan,BLOCKS
        from qwen_asr_app.review_notes import validated_plan_part
        complete=plan();previous={**complete,'knowledge':complete['knowledge'][1:]}
        package={'sources':[],'associations':[],'relevant_pages':[],'history_scope':'原历史范围',
                 'learning_scope':lesson_scope(),'_scope_transcript_blocks':BLOCKS}
        payload={'transcript':BLOCKS,'history':[]};addition={**complete['knowledge'][0]}
        addition.pop('parameter_only_objective_ids')
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            (folder/'coverage-validation-0-attempt-0.json').write_text(json.dumps({
                'attempt':0,'max_repairs':2,'proposal':previous,
                'validation_error':'本节涉及的掌握要求缺少知识覆盖：gain'}))
            with patch('qwen_asr_app.review_notes.request_ai_text',AsyncMock(side_effect=[
                '{"knowledge":[]}',json.dumps({'knowledge':[addition]})])) as request:
                result=await validated_plan_part('知识清单',payload,config(),{'S1'},package,folder,0)
            self.assertEqual(request.await_count,2)
            failed=json.loads((folder/'coverage-validation-0-attempt-1.json').read_text())
            self.assertEqual(failed['proposal'],previous)
            self.assertEqual(result['knowledge'][0],previous['knowledge'][0])

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
