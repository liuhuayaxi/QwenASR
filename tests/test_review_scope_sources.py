from tests import isolation
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qwen_asr_app.review_materials import substantive_page_text, learning_scope_candidates
from qwen_asr_app.note_pdf import reviewed_pdf
from qwen_asr_app.settings import DEFAULT_NOTE_SUMMARY_PROMPT
from qwen_asr_app.review_scope import canonical_metadata,validate_plan
from qwen_asr_app.review_notes import summary_signature


class LearningScopeSourceTests(unittest.TestCase):
    def test_canonical_depth_comes_from_source_ids_and_keeps_mixed_items_invalid(self):
        scope={'enabled':True,'items':[{'id':'m','level':'mastery'},{'id':'f','level':'familiarity'}]}
        original={'knowledge':[{'id':'k','objective_ids':['f'],'scope_level':'mastery','formula_policy':'required_as_needed'}]}
        fixed=canonical_metadata(original,scope)
        self.assertEqual(original['knowledge'][0]['scope_level'],'mastery')
        self.assertEqual(fixed['knowledge'][0]['scope_level'],'familiarity')
        self.assertEqual(fixed['knowledge'][0]['formula_policy'],'no_standalone_formulas')
        mixed={'knowledge':[{'id':'k','objective_ids':['m','f'],'sources':['S1'],'scope_level':'mastery','formula_policy':'required_as_needed'}],
            'objective_coverage':[{'objective_id':i,'relevance':'relevant','sources':['S1'],'evidence_quote':'真实原文'} for i in ['m','f']]}
        with self.assertRaisesRegex(ValueError,'拆分'):
            validate_plan(canonical_metadata(mixed,scope),{'S1'},scope,[{'source':'S1','text':'真实原文'}])

    def test_unrelated_live_model_changes_do_not_invalidate_summary_stage(self):
        import copy
        a={'ai':{'transport':'agy_cli','model':'live-low','cli_model':'live-low',
                 'summary_provider':{'transport':'agy_cli','model':'summary-high','cli_model':'summary-high','cli_effort':'high'},
                 'review_provider':{'model':'review-high'}},'_note_summary_system_prompt':'既有摘要规则'}
        b=copy.deepcopy(a);b['ai']['model']=b['ai']['cli_model']='live-medium';b['ai']['review_provider']['model']='other-review'
        b['inference_settings']={'asr_provider':'remote_only'}
        self.assertEqual(summary_signature(a),summary_signature(b))
        b['ai']['summary_provider']['cli_model']='other-summary'
        self.assertNotEqual(summary_signature(a),summary_signature(b))

    def test_scan_page_number_and_filename_are_not_native_teaching_text(self):
        self.assertEqual(substantive_page_text('《 一个超过四十字的原课件文件名称和章节标题 》      - 32/106页 -'), '')
        self.assertEqual(substantive_page_text('- 2/113页 -\n《 第一章 》\n'), '')
        self.assertIn('了解光场限制因子', substantive_page_text('《 第三章 》 - 32/106页 -\n了解光场限制因子'))

    def test_section_end_requirements_are_found_without_loading_whole_pdf_images(self):
        source={'source_id':'source-a','sha256':'a'*64,'pages':[
            {'page':1,'text':'第三章目录'}, {'page':2,'text':'普通概念正文'},
            {'page':32,'text':'[TEXT]\n3.2节要求\n[VISUAL_ONLY]','image_primary':True}]}
        candidates=learning_scope_candidates([source])
        self.assertEqual([c['page'] for c in candidates], [32])
        self.assertEqual(candidates[0]['source_sha256'], 'a'*64)

    def test_visible_defaults_explicitly_state_optoelectronics_depth_rules(self):
        self.assertIn('光电子学专项要求', DEFAULT_NOTE_SUMMARY_PROMPT)
        self.assertIn('必须先阅读每章 PPT', DEFAULT_NOTE_SUMMARY_PROMPT)
        self.assertIn('了解项只作简短概念说明', DEFAULT_NOTE_SUMMARY_PROMPT)
        source=(Path(__file__).resolve().parents[1]/'static/js/default-prompts.js').read_text()
        browser_value=json.loads(source.split('=',1)[1].strip().removesuffix(';'))
        self.assertEqual(browser_value,DEFAULT_NOTE_SUMMARY_PROMPT)

    def test_scope_review_cannot_reuse_generic_pass_or_different_objective_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'.review-notes/task'
            folder.mkdir(parents=True)
            pdf=folder/'candidate.pdf';pdf.write_bytes(b'fixture')
            record=SimpleNamespace(directory=root,metadata={'ai_note_summary':{
                'summary':'原正文','review_package':'.review-notes/task',
                'material_version':{'learning_scope':{'enabled':True,'sha256':'scope-v1'}}}})
            report={'status':'passed','pdf_sha256':'pdf-v1','viewed_pages':[1],
                'policy':'visual-pages-and-originals-v3'}
            with patch('qwen_asr_app.note_pdf.export_pdf',return_value=(pdf,{'pdf_sha256':'pdf-v1','pages':1})):
                (folder/'review.json').write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError,'学习要求'):reviewed_pdf(record)
                report.update(policy='visual-pages-and-originals-scope-v4',scope_review_passed=True,learning_scope_sha256='other-scope')
                (folder/'review.json').write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError,'学习要求'):reviewed_pdf(record)
                report['learning_scope_sha256']='scope-v1'
                (folder/'review.json').write_text(json.dumps(report))
                self.assertEqual(reviewed_pdf(record),pdf)


if __name__=='__main__':unittest.main()
