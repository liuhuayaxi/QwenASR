from tests import isolation  # noqa: F401
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from qwen_asr_app.note_review_repair import apply_patches,repair_review,queue_independent_recheck,issue_groups,install_repair,resume_install,reserve_action
from qwen_asr_app.note_latex_author import _binding


class LocalPatchTests(unittest.TestCase):
    def test_interrupted_source_install_finishes_on_resume_without_overwriting_edits(self):
        from qwen_asr_app.durable import atomic_bytes
        for concurrent_edit in (False,True):
            with self.subTest(concurrent_edit=concurrent_edit),tempfile.TemporaryDirectory() as raw:
                folder=Path(raw);(folder/'latex').mkdir()
                for relative in ('note.md','latex/body.tex','latex/source.json'):(folder/relative).write_text('old')
                writes=0
                def interrupted(path,data):
                    nonlocal writes
                    writes+=1
                    if writes==2:raise OSError('synthetic shutdown between atomic writes')
                    atomic_bytes(path,data)
                with patch('qwen_asr_app.note_review_repair.atomic_bytes',side_effect=interrupted):
                    with self.assertRaises(OSError):install_repair(folder,'new markdown','new latex',{'version':'new'})
                self.assertEqual((folder/'note.md').read_text(),'new markdown')
                self.assertEqual((folder/'latex/body.tex').read_text(),'old')
                if concurrent_edit:
                    (folder/'latex/body.tex').write_text('user edited')
                    with self.assertRaisesRegex(ValueError,'用户修改'):resume_install(folder)
                    self.assertEqual((folder/'latex/body.tex').read_text(),'user edited')
                else:
                    resume_install(folder)
                    self.assertEqual((folder/'latex/body.tex').read_text(),'new latex')
                    self.assertEqual(json.loads((folder/'latex/source.json').read_text()),{'version':'new'})
                    self.assertEqual(json.loads((folder/'automatic-repair/install.json').read_text())['state'],'installed')

    def test_restarts_reuse_action_identity_without_refilling_the_round_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            reserve_action(raw,'first','patch','same-source')
            reserve_action(raw,'first','patch','same-source')
            reserve_action(raw,'second','recheck','same-source')
            with self.assertRaisesRegex(ValueError,'两次上限'):reserve_action(raw,'third','patch','same-source')
            reserve_action(raw,'third','patch','same-source',retry_round='explicit-user-retry')

    def test_global_scope_dispatch_finding_routes_to_its_real_source_page(self):
        groups=issue_groups([{'page':1,'problem':'本节学习要求未完成全文深度核对','evidence':'threshold-state'}],
            [{'page':1,'text':'S1 初步概念'},{'page':23,'text':'S72 阈值条件的物理意义'}],
            {'objective_coverage':[{'objective_id':'threshold-state','sources':['S72']}]})
        self.assertEqual(list(groups),[23]);self.assertEqual(groups[23][0]['reported_page'],1)

    def test_patch_cannot_move_sources_images_or_knowledge_boundaries(self):
        body='% knowledge:k\n说明'+r'\SourceRef{1}\CourseFigure{assets/fig-1234567890abcdef.png}{图注}{说明}'+'\n% /knowledge:k\n'
        for old,new in [(r'\SourceRef{1}',r'\SourceRef{2}'),('assets/fig-1234567890abcdef.png','assets/fig-abcdef1234567890.png')]:
            with self.assertRaisesRegex(ValueError,'来源或原图'):apply_patches(body,[{'knowledge_id':'k','old':old,'new':new}],{'k'},'latex')
        with self.assertRaisesRegex(ValueError,'唯一匹配'):apply_patches(body,[{'knowledge_id':'k','old':'说明','new':'另一说明'}],{'k'},'latex')
        with self.assertRaisesRegex(ValueError,'范围'):apply_patches(body,[{'knowledge_id':'other','old':'图注','new':'准确图注'}],{'k'},'latex')
        result=apply_patches(body,[{'knowledge_id':'k','old':'图注','new':'准确图注'}],{'k'},'latex')
        self.assertIn('{准确图注}',result)


class AutonomousRepairTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self,folder):
        markdown='<!-- knowledge:k -->正确工艺说明[原文 S1](#source-1)<!-- /knowledge:k -->'
        body=r'\CourseTitle{测试}{来源核对}'+'\n% knowledge:k\n错误工艺说明'+r'\SourceRef{1}'+'\n% /knowledge:k\n'
        (folder/'latex').mkdir();(folder/'latex/body.tex').write_text(body)
        provenance={'layout_engine':'xelatex-course-v1'}
        (folder/'latex/source.json').write_text(json.dumps({'body_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'binding':_binding(markdown,provenance),'writer_session_ids':['initial-writer']}))
        (folder/'transcript.json').write_text(json.dumps({'blocks':[{'start':0,'end':60,'text':'正确工艺说明'}]}))
        pdf=folder/'candidate.pdf';pdf.write_bytes(b'synthetic PDF; native extraction mocked')
        (folder/'export.json').write_text(json.dumps({'latex_body_sha256':hashlib.sha256(body.encode()).hexdigest()}))
        digest=hashlib.sha256(pdf.read_bytes()).hexdigest();pages=folder/'pages'/digest;pages.mkdir(parents=True)
        image=pages/'page-001.png';image.write_bytes(b'synthetic actual-page marker')
        report={'pdf_sha256':digest,'issues':[{'page':1,'severity':'warning','problem':'工艺错误','evidence':'本页与来源冲突'}],
            'reviewer_session_ids':['initial-reviewer'],'batches':[{'pages':[1],'receipt':{'required_images':[str(image)]}}]}
        ledger={'knowledge':[{'id':'k','sources':['S1']}]}
        return markdown,provenance,pdf,report,ledger,body

    def result(self,decision='fix',replacement='正确工艺说明'):
        return {'decisions':[{'issue_index':0,'decision':decision,'evidence':'已对照本页与课堂原文'}],
            'latex_patches':[{'knowledge_id':'k','old':'错误工艺说明','new':replacement}] if decision=='fix' else [],'markdown_patches':[]}

    async def test_independent_model_repairs_locally_preserving_markdown_and_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            f=Path(raw);md,p,pdf,report,ledger,body=self.fixture(f)
            request=AsyncMock(return_value=(json.dumps(self.result()),{'session_id':'repairer','viewed_files':[]}))
            with patch('qwen_asr_app.note_review_repair.pdf_operation',return_value=[{'page':1,'text':'S1 工艺'}]),patch('qwen_asr_app.note_review_repair.request_materials',request):
                result=await repair_review(md,f,p,{},ledger,{'learning_scope':{'enabled':False}},[],pdf,report,generator_session_ids=['initial-writer'])
            self.assertTrue(result['changed']);self.assertEqual(result['markdown'],md)
            self.assertNotIn('错误工艺说明',(f/'latex/body.tex').read_text())
            manifest=json.loads((f/'latex/source.json').read_text())
            self.assertEqual(manifest['writer_session_ids'],['initial-writer','repairer'])
            self.assertEqual(manifest['binding'],_binding(md,p))
            self.assertEqual(report['issues'][0]['severity'],'warning')  # No invented pass flag.
            self.assertTrue(list((f/'automatic-repair').glob('*/before.tex')))

    async def test_false_positive_is_evidence_backed_without_rewriting(self):
        with tempfile.TemporaryDirectory() as raw:
            f=Path(raw);md,p,pdf,report,ledger,body=self.fixture(f)
            with patch('qwen_asr_app.note_review_repair.pdf_operation',return_value=[{'page':1,'text':'S1 工艺'}]),patch('qwen_asr_app.note_review_repair.request_materials',new=AsyncMock(return_value=(json.dumps(self.result('false_positive')),{'session_id':'repairer'}))):
                result=await repair_review(md,f,p,{},ledger,{},[],pdf,report)
            self.assertFalse(result['changed']);self.assertFalse(result['unresolved'])
            self.assertEqual((f/'latex/body.tex').read_text(),body)

    async def test_unsafe_proposals_exhaust_finite_budget_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as raw:
            f=Path(raw);md,p,pdf,report,ledger,body=self.fixture(f)
            request=AsyncMock(return_value=(json.dumps(self.result(replacement=r'\input{private}')),{'session_id':'repairer'}))
            with patch('qwen_asr_app.note_review_repair.pdf_operation',return_value=[{'page':1,'text':'S1 工艺'}]),patch('qwen_asr_app.note_review_repair.request_materials',request):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError,'三次提案'):await repair_review(md,f,p,{},ledger,{},[],pdf,report)
            self.assertEqual(request.await_count,3)
            self.assertEqual((f/'latex/body.tex').read_text(),body)

    async def test_final_round_can_adjudicate_but_cannot_install_unreviewed_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            f=Path(raw);md,p,pdf,report,ledger,body=self.fixture(f)
            with patch('qwen_asr_app.note_review_repair.pdf_operation',return_value=[{'page':1,'text':'S1 工艺'}]),patch('qwen_asr_app.note_review_repair.request_materials',new=AsyncMock(return_value=(json.dumps(self.result()),{'session_id':'repairer'}))):
                with self.assertRaisesRegex(ValueError,'达到上限'):await repair_review(md,f,p,{},ledger,{},[],pdf,report,allow_changes=False)
            self.assertEqual((f/'latex/body.tex').read_text(),body)

    async def test_blocking_finding_cannot_be_self_approved_as_a_false_positive(self):
        with tempfile.TemporaryDirectory() as raw:
            f=Path(raw);md,p,pdf,report,ledger,body=self.fixture(f)
            report['issues'][0]['severity']='blocking'
            with patch('qwen_asr_app.note_review_repair.pdf_operation',return_value=[{'page':1,'text':'S1 工艺'}]),patch('qwen_asr_app.note_review_repair.request_materials',new=AsyncMock(return_value=(json.dumps(self.result('false_positive')),{'session_id':'repairer'}))):
                result=await repair_review(md,f,p,{},ledger,{},[],pdf,report)
            self.assertFalse(result['changed']);self.assertEqual(result['recheck_pages'],[1])
            # The repairer cannot change the blocking verdict to passed.
            self.assertEqual(report['issues'][0]['severity'],'blocking')

    async def test_disputed_blocker_requires_a_new_reviewer_and_preserves_original_report(self):
        with tempfile.TemporaryDirectory() as raw:
            f=Path(raw);md,p,pdf,report,ledger,body=self.fixture(f)
            report['status']='needs_revision';report['viewed_pages']=[1]
            report['batches'][0].update(receipt={'session_id':'original-reviewer'},result={'issues':report['issues']})
            queue_independent_recheck(pdf,report,[1],'automatic-audit.json')
            queued=json.loads((f/'review.json').read_text())
            self.assertEqual(queued['status'],'unreviewed');self.assertEqual(queued['viewed_pages'],[])
            self.assertEqual(queued['disputed_reviewer_session_ids'],['original-reviewer'])
            self.assertTrue(list(f.glob('review-before-adjudication-*.json')))

    async def test_page_and_source_version_mismatch_stops_before_model_call(self):
        with tempfile.TemporaryDirectory() as raw:
            f=Path(raw);md,p,pdf,report,ledger,body=self.fixture(f)
            (f/'export.json').write_text(json.dumps({'latex_body_sha256':'0'*64}))
            with patch('qwen_asr_app.note_review_repair.request_materials',new=AsyncMock()) as request:
                with self.assertRaisesRegex(ValueError,'源稿版本'):await repair_review(md,f,p,{},ledger,{},[],pdf,report)
            request.assert_not_awaited()
