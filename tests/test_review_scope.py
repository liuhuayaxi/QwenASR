from tests import isolation  # noqa: F401
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from qwen_asr_app import review_scope as scope
from qwen_asr_app.review_notes import validate_plan as validate_pipeline_plan


def lesson_scope():
    return {'enabled':True,'version':scope.VERSION,'items':[
        {'id':'gain','text':'掌握阈值增益','level':'mastery','page':2},
        {'id':'gamma','text':'了解光限制因子','level':'familiarity','page':2},
        {'id':'future','text':'掌握下一章探测器原理','level':'mastery','page':2}]}


def plan():
    return {'knowledge':[
        {'id':'k1','topic':'阈值增益','kind':'new_development','sources':['S1'],'objective_ids':['gain'],
         'parameter_only_objective_ids':['gamma'],'scope_level':'mastery','formula_policy':'required_as_needed'},
        {'id':'k2','topic':'光限制因子','kind':'new_development','sources':['S1'],'objective_ids':['gamma'],
         'scope_level':'familiarity','formula_policy':'no_standalone_formulas'}],
        'figures':[],'objective_coverage':[
            {'objective_id':'gain','relevance':'relevant','sources':['S1'],'evidence_quote':'阈值增益'},
            {'objective_id':'gamma','relevance':'relevant','sources':['S1'],'evidence_quote':'光限制因子'},
            {'objective_id':'future','relevance':'not_this_part','sources':[],'reason':'本课没有涉及探测器'}]}


BLOCKS = [{'source':'S1','text':'本课讲阈值增益，光限制因子只需知道含义。'}]


class ScopePlanTests(unittest.TestCase):
    def validate(self, value): return scope.validate_plan(value,{'S1'},lesson_scope(),BLOCKS)

    def test_mastery_parameter_symbol_does_not_force_familiarity_derivation(self):
        self.validate(plan())

    def test_future_mastery_is_not_forced_into_current_lesson(self):
        value=plan();self.validate(value)
        self.assertFalse(any('future' in k['objective_ids'] for k in value['knowledge']))

    def test_relevant_mastery_cannot_disappear_from_plan(self):
        value=plan();value['knowledge']=value['knowledge'][1:]
        with self.assertRaisesRegex(ValueError,'掌握要求缺少'):self.validate(value)

    def test_fabricated_objective_id_and_quote_are_rejected(self):
        value=plan();value['knowledge'][0]['objective_ids']=['invented']
        with self.assertRaisesRegex(ValueError,'无效学习要求'):self.validate(value)
        value=plan();value['objective_coverage'][0]['evidence_quote']='本节完全不存在的句子'
        with self.assertRaisesRegex(ValueError,'逐字课堂证据'):self.validate(value)

    def test_familiarity_cannot_be_upgraded_or_mixed_into_mastery(self):
        for mutation in ('upgrade','formula','mix'):
            value=plan()
            if mutation=='upgrade':value['knowledge'][1]['scope_level']='mastery'
            elif mutation=='formula':value['knowledge'][1]['formula_policy']='required_as_needed'
            else:value['knowledge'][0]['objective_ids'].append('gamma')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.validate(value)

    def test_every_original_requirement_needs_explicit_relevance(self):
        value=plan();value['objective_coverage'].pop()
        with self.assertRaisesRegex(ValueError,'覆盖清单缺少'):self.validate(value)

    def test_familiarity_only_figure_is_rejected(self):
        value=plan();value['figures']=[{'objective_ids':['gamma']}]
        with self.assertRaisesRegex(ValueError,'独立教学图'):self.validate(value)

    def test_existing_plan_validator_enforces_scope(self):
        value=plan();value['knowledge'][1]['scope_level']='mastery'
        package={'sources':[],'learning_scope':lesson_scope(),'_scope_transcript_blocks':BLOCKS}
        with self.assertRaisesRegex(ValueError,'公式策略'):validate_pipeline_plan(value,{'S1'},package)

    def test_merged_parts_retain_relevance_and_source_evidence(self):
        other=copy.deepcopy(plan())
        for row in other['objective_coverage']:row.update(relevance='not_this_part',sources=[],evidence_quote='')
        merged=scope.merge_coverage([other,plan()],lesson_scope())
        self.assertEqual(merged[0]['relevance'],'relevant');self.assertEqual(merged[0]['sources'],['S1'])
        self.assertEqual(merged[-1]['relevance'],'not_this_part')


class ScopeBodyAndReviewTests(unittest.TestCase):
    def test_understand_only_prose_and_mastery_formula_pass(self):
        markdown='<!-- knowledge:k1 -->阈值增益定义，Gamma只作为必要参数。 $$g=1/\\Gamma$$<!-- /knowledge:k1 -->\n<!-- knowledge:k2 -->光限制因子描述光场与增益区的重叠程度。<!-- /knowledge:k2 -->'
        self.assertEqual(scope.depth_issues(markdown,plan(),lesson_scope()),[])

    def test_formula_bleed_and_formula_outside_scoped_blocks_are_blocking(self):
        markdown='<!-- knowledge:k1 -->阈值条件<!-- /knowledge:k1 -->\n<!-- knowledge:k2 -->$$\\Gamma=\\int E^2$$<!-- /knowledge:k2 -->'
        self.assertTrue(any('了解项' in i['problem'] for i in scope.depth_issues(markdown,plan(),lesson_scope())))
        markdown='<!-- knowledge:k1 -->阈值条件<!-- /knowledge:k1 -->\n<!-- knowledge:k2 -->概念<!-- /knowledge:k2 -->\n$$J_{th}=1$$'
        self.assertTrue(any('未关联' in i['problem'] for i in scope.depth_issues(markdown,plan(),lesson_scope())))

    def test_missing_or_duplicate_knowledge_blocks_fail(self):
        for markdown in ('no markers','<!-- knowledge:k1 -->a<!-- /knowledge:k1 -->'*2):
            self.assertTrue(scope.depth_issues(markdown,plan(),lesson_scope()))

    def test_generic_passed_flag_does_not_count_as_scope_review(self):
        with self.assertRaisesRegex(ValueError,'学习要求深度核对'):
            scope.validate_review({'status':'passed'},lesson_scope(),['gain'])

    def test_scope_violation_forces_revision_even_when_model_says_passed(self):
        result={'status':'passed','viewed_pages':[4], 'issues':[], 'scope_policy_checked':True,
                'scope_findings':[{'objective_id':'gamma','status':'familiarity_expanded','page':4,'evidence':'第4页包含独立Gamma积分推导'}]}
        scope.validate_review(result,lesson_scope(),['gamma'])
        self.assertEqual(result['status'],'needs_revision');self.assertEqual(result['issues'][0]['severity'],'blocking')

    def test_review_cannot_omit_relevant_objective_id(self):
        with self.assertRaisesRegex(ValueError,'遗漏本批'):
            scope.validate_review({'scope_policy_checked':True,'scope_findings':[]},lesson_scope(),['gain'])


class ScopeLatexBodyTests(unittest.TestCase):
    def body(self, familiarity='光限制因子描述重叠程度，符号为 $\\Gamma$。', mastery=None):
        return ('% knowledge:k1\n'+(mastery or '阈值增益：\\[g=1/\\Gamma\\]')+
                '\n% /knowledge:k1\n% knowledge:k2\n'+familiarity+'\n% /knowledge:k2\n')

    def check(self, body, ledger=None):
        return scope.depth_issues(body,ledger or plan(),lesson_scope(),source_format='latex')

    def test_mastery_formulas_and_simple_familiarity_parameter_symbols_pass(self):
        text=self.body(r'光限制因子用 \(\Gamma\) 表示；阈值电流密度记为 $J_{th}$。',
                       r'阈值增益满足 \begin{equation}g=\frac{1}{\Gamma}\end{equation}')
        self.assertEqual(self.check(text),[])

    def test_familiarity_display_math_in_each_common_environment_is_blocking(self):
        for formula in (r'$$\Gamma=1$$',r'\[\Gamma=1\]',
                        r'\begin{equation*}\Gamma=1\end{equation*}',
                        r'\begin{align}\Gamma&=1\end{align}',
                        r'\begin{gather*}\Gamma=1\end{gather*}'):
            with self.subTest(formula=formula):
                self.assertTrue(any('了解项包含独立公式' in i['problem'] for i in self.check(self.body(formula))))

    def test_familiarity_inline_relations_fractions_and_integrals_are_blocking(self):
        for formula in (r'$\Gamma=1$',r'\(\Gamma\approx 1\)',r'$\frac{E_a}{E}$',
                        r'\(\int E^2\,dx\)',r'$\dfrac{1}{2}$',r'\(x\le 1\)',
                        r'\ensuremath{\frac{1}{2}}'):
            with self.subTest(formula=formula):
                self.assertTrue(any('了解项包含独立公式' in i['problem'] for i in self.check(self.body(formula))))

    def test_unconfirmed_latex_cannot_expand_into_formula_or_image(self):
        ledger=plan();ledger['knowledge'][1]['scope_level']='unconfirmed'
        issues=self.check(self.body(r'\(J=\frac{I}{A}\)\CourseFigure{fig}{图}{说明}'),ledger)
        self.assertTrue(any('范围待核对项包含独立公式' in i['problem'] for i in issues))
        self.assertTrue(any('范围待核对项展开了独立教学图' in i['problem'] for i in issues))

    def test_familiarity_latex_images_are_blocking(self):
        for image in (r'\CourseFigure{fig}{图}{说明}',r'\includegraphics[width=\linewidth]{asset.png}'):
            with self.subTest(image=image):
                self.assertTrue(any('了解项展开了独立教学图' in i['problem'] for i in self.check(self.body(image))))

    def test_display_outside_blocks_is_blocking_but_no_blanket_fraction_scan(self):
        for formula in (r'\[g=1\]',r'$$g=1$$',r'\begin{align*}g&=1\end{align*}'):
            self.assertTrue(any('未关联' in i['problem'] for i in self.check(self.body()+'\n'+formula)))
        self.assertEqual(self.check(self.body()+'\n'+r'\newcommand{\CoreRatio}{\frac{x}{y}}'),[])

    def test_tex_comments_are_ignored_and_escaped_percent_keeps_real_math(self):
        self.assertEqual(self.check(self.body('% 不输出的草稿公式 $$Gamma=1$$ 和 \\includegraphics{x}\n只讲含义。')+
                                    '\n% 块外注释中的 \\[x=1\\] 不会排版。'),[])
        issues=self.check(self.body(r'效率为 10\%，但 \(\Gamma=1\) 仍是公式。'))
        self.assertTrue(any('了解项包含独立公式' in i['problem'] for i in issues))
        self.assertTrue(any('没有正文内容' in i['problem'] for i in self.check(self.body('% 只有注释'))))

    def test_missing_duplicate_mismatched_unknown_and_nested_latex_markers_fail(self):
        valid=self.body()
        variants=(valid.replace('% /knowledge:k2',''),valid+valid,
                  valid.replace('% /knowledge:k2','% /knowledge:k1'),
                  valid+'% knowledge:invented\n额外内容\n% /knowledge:invented\n',
                  '% knowledge:k1\n阈值\n% knowledge:k2\n概念\n% /knowledge:k2\n% /knowledge:k1\n')
        for value in variants:
            with self.subTest(value=value):self.assertTrue(self.check(value))

    def test_markdown_nested_markers_cannot_hide_familiarity_in_mastery(self):
        markdown='<!-- knowledge:k1 -->阈值<!-- knowledge:k2 -->概念<!-- /knowledge:k2 --><!-- /knowledge:k1 -->'
        self.assertTrue(any('嵌套' in i['problem'] for i in scope.depth_issues(markdown,plan(),lesson_scope())))


class ScopeCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_course_never_calls_extra_model_or_reads_original(self):
        with patch.object(scope,'request_materials',AsyncMock()) as request:
            self.assertFalse((await scope.prepare({'category':'通信原理 C'},Path('/unused'),{}))['enabled'])
            request.assert_not_awaited()

    async def test_hash_bound_visual_cache_is_reused_and_stale_sha_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source_file=root/'original.pdf';source_file.write_bytes(b'original fixture bytes')
            sha=hashlib.sha256(source_file.read_bytes()).hexdigest()
            source={'source_id':'s','sha256':sha,'path':'original.pdf','name':'chapter.pdf','pages':[{},{}]}
            package={'category':'光电子学','course_id':'optics','sources':[source]}
            cached={'version':scope.VERSION,'source_sha256':sha,'items':[{'id':'a','page':2,'text':'了解光限制因子','level':'familiarity'}],
                    'read_pages':[2],'receipt':{'verification_method':'codex_visual_read','page_images':[{'page':2,'sha256':'b'*64}]}}
            cache_dir=root/'private/review-materials-cache'/scope.VERSION;cache_dir.mkdir(parents=True)
            cache_path=cache_dir/(sha+'.json');cache_path.write_text(json.dumps(cached))
            with patch.object(scope,'PRIVATE_ROOT',root/'private'),patch.object(scope,'request_materials',AsyncMock()) as request:
                actual=await scope.prepare(package,root,{})
                self.assertEqual(actual['items'][0]['course_id'],'optics');request.assert_not_awaited()
                cached['source_sha256']='c'*64;cache_path.write_text(json.dumps(cached))
                with self.assertRaisesRegex(ValueError,'版本不一致'):await scope.prepare(package,root,{})

    async def test_cache_without_actual_page_receipt_is_rejected(self):
        source={'sha256':'a'*64,'pages':[{}]}
        value={'version':scope.VERSION,'source_sha256':'a'*64,'items':[],'read_pages':[1],'receipt':{'verification_method':'codex_visual_read','page_images':[]}}
        with self.assertRaisesRegex(ValueError,'逐页图像摘要'):scope.validate_cache(value,source)


class ScopeVisualGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_generic_review_cannot_bypass_scope_and_real_body_gate(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from qwen_asr_app.review_notes import visual_review
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);pdf=root/'candidate.pdf';pdf.write_bytes(b'synthetic PDF identity, no actual PDF read')
            digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
            (root/'learning-scope.json').write_text(json.dumps(lesson_scope()))
            (root/'transcript.json').write_text(json.dumps({'blocks':BLOCKS,'segments':[]}))
            # Legacy pass lacks the objective policy and must never short-circuit.
            (root/'review.json').write_text(json.dumps({'policy':'visual-pages-and-originals-v3','status':'passed','pdf_sha256':digest}))
            markdown='<!-- knowledge:k1 -->阈值增益 $$g=1/\\Gamma$$<!-- /knowledge:k1 -->\n<!-- knowledge:k2 -->$$\\Gamma=\\int E^2$$<!-- /knowledge:k2 -->'
            (root/'note.md').write_text(markdown)
            page=SimpleNamespace(get_text=lambda:'S1 阈值增益 光限制因子',get_image_info=lambda **kw:[],
                get_pixmap=lambda **kw:SimpleNamespace(save=lambda path:Path(path).write_bytes(b'no rendering; fake test pixels')))
            response={'viewed_pages':[1],'status':'passed','issues':[],'scope_policy_checked':True,
                      'scope_findings':[{'objective_id':i,'status':'appropriate','page':1,'evidence':'本页相应知识内容'} for i in ('gain','gamma')]}
            request=AsyncMock(return_value=(json.dumps(response),{'session_id':'independent-new-session'}))
            def pdf_worker(operation,**kwargs):
                if operation=='image_locations':return {}
                if operation=='render':
                    for selection in kwargs['pages']:page.get_pixmap().save(selection['output'])
                    return [{'page':selection['page'],'text':page.get_text()} for selection in kwargs['pages']]
                raise AssertionError(operation)
            with patch('qwen_asr_app.native_pdf.pdf_operation',side_effect=pdf_worker),patch('qwen_asr_app.review_notes.request_materials',request):
                report=await visual_review(pdf,{'pages':1,'pdf_sha256':digest,'renderer':'fake'},root,{},plan(),[],generator_session_ids=['writer'])
            request.assert_awaited_once()
            self.assertEqual(report['policy'],scope.REVIEW_POLICY)
            self.assertEqual(report['learning_scope_sha256'],scope.scope_hash(lesson_scope()))
            self.assertEqual(report['status'],'needs_revision')
            self.assertFalse(report['scope_review_passed'])
            self.assertTrue(any('了解项' in issue['problem'] for issue in report['scope_depth_issues']))


class BoundedPlanRepairTests(unittest.IsolatedAsyncioTestCase):
    def package(self):
        return {'sources':[],'learning_scope':lesson_scope(),'_scope_transcript_blocks':BLOCKS}

    def invalid(self):
        value=plan();value['objective_coverage'][0]['evidence_quote']='本节介绍了激光阈值和限制因子'
        return value

    async def test_paraphrase_is_repaired_using_original_literal_quote(self):
        from qwen_asr_app.review_notes import validated_plan_part
        invalid=self.invalid();correct=plan();payload={'transcript':BLOCKS,'learning_scope':lesson_scope()}
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            model=AsyncMock(side_effect=[invalid,correct])
            with patch('qwen_asr_app.review_notes.model_json',model):
                result=await validated_plan_part('plan instructions',payload,{'saved_model':'unchanged'},{'S1'},self.package(),root,0)
            self.assertEqual(result['knowledge'],invalid['knowledge'])
            self.assertEqual(result['objective_coverage'][0]['evidence_quote'],'阈值增益')
            self.assertEqual(model.await_count,2)
            repair=model.await_args_list[1]
            self.assertEqual(repair.args[1]['source_input'],payload)
            self.assertEqual(repair.args[1]['invalid_proposal'],invalid)
            self.assertEqual(repair.args[2],{'saved_model':'unchanged'})
            evidence=json.loads((root/'coverage-validation-0-attempt-0.json').read_text())
            self.assertEqual(evidence['diagnostics']['objective_id'],'gain')
            self.assertEqual(evidence['diagnostics']['source_ids'],['S1'])
            self.assertEqual(evidence['proposal'],invalid)

    async def test_invalid_repairs_are_bounded_persisted_and_not_repeated_on_resume(self):
        from qwen_asr_app.review_notes import validated_plan_part
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);accepted=root/'coverage-part-0.json';accepted.write_text('{"successful":"preserved"}')
            before=accepted.read_bytes();model=AsyncMock(side_effect=[self.invalid(),self.invalid(),self.invalid()])
            with patch('qwen_asr_app.review_notes.model_json',model):
                with self.assertRaisesRegex(ValueError,'仍未通过校验'):
                    await validated_plan_part('instructions',{'transcript':BLOCKS},{},{'S1'},self.package(),root,1)
            self.assertEqual(model.await_count,3)
            self.assertEqual(len(list(root.glob('coverage-validation-1-attempt-*.json'))),3)
            self.assertEqual(accepted.read_bytes(),before)
            with patch('qwen_asr_app.review_notes.model_json',AsyncMock()) as resumed:
                with self.assertRaisesRegex(ValueError,'自动修复上限'):
                    await validated_plan_part('instructions',{'transcript':BLOCKS},{},{'S1'},self.package(),root,1)
                resumed.assert_not_awaited()

    async def test_interrupted_repair_resumes_from_last_invalid_checkpoint(self):
        from qwen_asr_app.review_notes import validated_plan_part
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            with patch('qwen_asr_app.review_notes.model_json',AsyncMock(side_effect=[self.invalid(),RuntimeError('temporary transport')])):
                with self.assertRaisesRegex(RuntimeError,'temporary transport'):
                    await validated_plan_part('instructions',{'transcript':BLOCKS},{},{'S1'},self.package(),root,2)
            with patch('qwen_asr_app.review_notes.model_json',AsyncMock(return_value=plan())) as resumed:
                result=await validated_plan_part('instructions',{'transcript':BLOCKS},{},{'S1'},self.package(),root,2)
            self.assertEqual(result['knowledge'],plan()['knowledge'])
            self.assertEqual(resumed.await_count,1)
            self.assertEqual(resumed.await_args.args[1]['repair_attempt'],1)

    def test_literal_quote_cannot_be_assembled_across_two_source_blocks(self):
        value=plan();value['objective_coverage'][0].update(sources=['S1','S2'],evidence_quote='增益光限制')
        with self.assertRaisesRegex(ValueError,'某一个S来源'):
            scope.validate_plan(value,{'S1','S2'},lesson_scope(),[
                {'source':'S1','text':'阈值增益'},{'source':'S2','text':'光限制因子'}])


if __name__=='__main__':unittest.main()
