import tests.isolation  # noqa: F401
import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_knowledge import FakeEmbedder, MockCourseFiles, MockRecordingStore
from qwen_asr_app.knowledge.service import KnowledgeService
from qwen_asr_app.knowledge.ingest import CourseIngestor
from qwen_asr_app.classroom_intelligence.service import ClassroomIntelligenceService
from qwen_asr_app.classroom_intelligence.store import ClassroomNoteStore
from qwen_asr_app.transcription_assist.validation import correction_risks


class KnowledgeContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.settings=SimpleNamespace(storage_root=self.root,known_roots=[str(self.root)],categories=['数学','物理'])
        self.files=MockCourseFiles(self.root)
        self.records=MockRecordingStore(self.root)
        self.model=FakeEmbedder(dim=32)
        self.k=KnowledgeService(self.settings,self.records,self.files,embedder=self.model)
        await self.k.start()
        self.addAsyncCleanup(self.k.close)

    def document(self,text='数值孔径 NA=0.20，功率为40 mW。'):
        path=self.root/'数学'/'讲义.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text(text,encoding='utf-8')
        self.files.add_file('数学','asset','讲义.md','讲义',path)
        return path

    async def test_real_citation_contract_can_be_accepted_and_undo_restores_sources(self):
        self.document('NA=0.20')
        self.k.sync_course('数学')
        evidence=self.k.search('数学','NA')[0]
        record=SimpleNamespace(id='classroom',directory=self.root/'lesson',duration=30,metadata={'category':'数学','title':'课堂'},segments=[])
        record.directory.mkdir()
        store=ClassroomNoteStore(record)
        note=await store.add_note('NA是什么意思',request_id='note-request-123',anchor='manual',start=0,end=20)
        service=ClassroomIntelligenceService(SimpleNamespace(find=lambda _:record),self.k)
        response={'claims':{'classroom':[],'material':[{'statement':evidence['text'],'quote':evidence['text'],'citation_id':evidence['citation_id']}],'ai_explanation':[],'check':{}}}
        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text',new=AsyncMock(return_value=json.dumps(response))):
            await service._run_enhancement(record,note['id'],force=True)
        generated=await store.get_note(note['id'])
        self.assertEqual(generated['suggestion']['validation']['verified_claims_count'],1)
        self.assertTrue(generated['suggestion']['evidence'][0]['chunk_id'])
        accepted=await store.apply_action(note['id'],'accept',generated['revision'],generated['suggestion']['id'],self.k)
        self.assertIn('NA=0.20',accepted['content'])
        self.assertEqual(accepted['raw'],'NA是什么意思')
        # A later stable ASR notification with no new evidence must not re-add the same note.
        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text',new=AsyncMock()) as repeat:
            await service._run_enhancement(record,note['id'])
            repeat.assert_not_awaited()
        edited=await store.patch_note(note['id'],accepted['revision'],content='我的独立判断')
        restored=await store.apply_action(note['id'],'undo',edited['revision'])
        self.assertEqual(restored['content'],accepted['content'])
        self.assertEqual(restored['evidence'],accepted['evidence'])
        await service.close()

    async def test_original_update_invalidates_unchanged_converted_pdf(self):
        import pymupdf
        original=self.document()
        pdf=self.root/'数学'/'converted.pdf'
        with pymupdf.open() as doc:
            page=doc.new_page();page.insert_text((30,60),'NA=0.20');doc.save(pdf)
        self.files.assets['数学']['asset']['pdf']=pdf
        self.files.assets['数学']['asset']['meta']['conversion']={'status':'completed','engine':'mineru-4-v1'}
        self.k.sync_course('数学')
        source=next(s for s in self.k.store.list_sources('数学') if s['source_type']=='course_file_pdf')
        refs=self.k.source_context('数学',source['source_id'],1)
        self.assertTrue(refs)
        original.write_text('数值孔径已更新为0.24。',encoding='utf-8')
        self.assertTrue(self.k.validate_evidence(refs)[0]['stale'])
        self.assertEqual(self.k.source_context('数学',source['source_id'],1),[])
        self.k.sync_course('数学')
        self.assertEqual(self.k.source_context('数学',source['source_id'],1),[])
        self.assertEqual(self.k.store.get_source(source['source_id'])['status'],'stale')
        with pymupdf.open() as doc:
            page=doc.new_page();page.insert_text((30,60),'NA=0.24');doc.save(pdf)
        self.k.sync_course('数学')
        self.assertIn('NA=0.24',self.k.source_context('数学',source['source_id'],1)[0]['text'])

    async def test_worker_scheduling_from_background_thread_recovers_and_finishes(self):
        self.document()
        ident=await asyncio.to_thread(self.k.schedule,'数学')
        for _ in range(100):
            job=self.k.store.get_job(ident)
            if job['status']=='completed':break
            await asyncio.sleep(.01)
        self.assertEqual(job['status'],'completed')
        self.assertTrue(self.k.search('数学','NA'))

    async def test_incremental_update_reuses_identical_vectors(self):
        class CountModel(FakeEmbedder):
            def __init__(self):super().__init__(dim=32);self.count=0
            def encode_documents(self,texts):self.count+=len(texts);return super().encode_documents(texts)
        model=CountModel();self.k.embedder=model
        path=self.document('# 第一章\n\n甲段包含数值孔径NA。\n\n# 第二章\n\n乙段包含功率40 mW。')
        self.k.sync_course('数学');first=model.count
        path.write_text(path.read_text()+'\n\n# 第三章\n\n丙段是新增内容。',encoding='utf-8')
        self.files.convert_file('数学', 'asset')
        self.k.sync_course('数学')
        self.assertLess(model.count-first,first+1)
        self.assertGreater(model.count,first)

    async def test_cancel_does_not_restart_on_new_transcript(self):
        record=SimpleNamespace(id='cancelled-note',directory=self.root/'cancelled',duration=30,metadata={'category':'数学'},segments=[{'id':1,'start':0,'end':30,'raw':'讲解NA'}])
        record.directory.mkdir();notes=ClassroomNoteStore(record)
        note=await notes.add_note('NA')
        await notes.apply_action(note['id'],'cancel',note['revision'])
        service=ClassroomIntelligenceService(SimpleNamespace(find=lambda _:record),None)
        with patch.object(service,'enqueue_enhancement',new=AsyncMock()) as enqueue:
            await service.on_transcript(record)
            enqueue.assert_not_awaited()
        await service.close()

    async def test_office_files_parse_xml_instead_of_binary_zip_bytes(self):
        file=self.root/'sample.pptx'
        with zipfile.ZipFile(file,'w') as z:
            z.writestr('ppt/slides/slide2.xml','<a:sld xmlns:a="urn:test"><a:p><a:t>NA=0.20</a:t></a:p></a:sld>')
            z.writestr('ppt/slides/slide10.xml','<a:sld xmlns:a="urn:test"><a:p><a:t>Power=40 mW</a:t></a:p></a:sld>')
        text,chunks=self.k.ingestor._load_file_content(file)
        self.assertIn('NA=0.20',text);self.assertIn('Power=40 mW',text)
        self.assertNotIn('PK',text)
        self.assertEqual([c.page for c in chunks],[1,2])

    def test_disfluency_cleanup_does_not_flip_negation_or_timestamp_numbers(self):
        self.assertEqual(correction_risks('[00:01:00–00:01:30]\n不仅如此，功率40 mW。','不仅如此，功率40 mW。'),[])
        self.assertTrue(correction_risks('相干解调不会出现门限效应','相干解调会出现门限效应'))
        self.assertTrue(correction_risks('条件x≤0.22','条件x≥0.22'))
        self.assertTrue(correction_risks('增益最大是三分之二。','增益最大是二分之三。'))
        self.assertTrue(correction_risks('条件是x大于零。','条件是x小于零。'))
        self.assertTrue(correction_risks('这是非相干解调。','这是相干解调。'))
        self.assertTrue(correction_risks('系统无噪声。','系统有噪声。'))
        self.assertEqual(correction_risks('这是一个重要概念。','这是重要概念。'),[])
        self.assertEqual(correction_risks('这是非常重要的概念。','这是特别重要的概念。'),[])

    def test_context_expansion_preserves_version_and_does_not_send_local_paths(self):
        self.document('# 公式\n\n数值孔径公式NA=0.20。'+('这里说明测量方法与应用范围。'*8)+'\n\n# 符号\n\n其中NA表示数值孔径，单位为一。')
        self.k.sync_course('数学')
        hit=self.k.search('数学','数值孔径公式',limit=1)[0]
        pack=self.k.evidence_pack([hit])
        self.assertGreaterEqual(len(pack),2)
        self.assertEqual(len({e['chunk_id'] for e in pack}),len(pack))
        self.assertTrue(all(e['revision']==hit['revision'] for e in pack))
        self.assertTrue(all('embedding_contract' not in e for e in pack))
        self.assertNotIn(str(self.root),json.dumps(pack))

    def test_answer_citations_are_checked_without_claiming_semantic_support(self):
        from qwen_asr_app.classroom_chat import validate_answer_sources
        answer,check=validate_answer_sources('依据 [K1] 和 [Kinvented]。',[{'citation_id':'K1'}],
            SimpleNamespace(validate_evidence=lambda refs:[dict(e,stale=True) for e in refs]))
        self.assertIn('[待核对来源]',answer)
        self.assertEqual(check['unknown_citations'],['Kinvented'])
        self.assertEqual(check['stale_citations'],['K1'])
        self.assertIn('不等于事实支持',check['semantic_support'])

    def test_one_oversize_structure_keeps_other_vectors_and_entire_text(self):
        class BudgetModel(FakeEmbedder):
            def encode_documents(self,texts):
                if any('OVERSIZE' in text for text in texts):
                    raise RuntimeError('超过模型输入预算')
                return super().encode_documents(texts)
        self.k.embedder=BudgetModel(dim=32)
        text='# 定义\n\n数值孔径NA。'+('定义和测量。'*18)+'\n\n# 整体公式\n\n$$OVERSIZE完整公式不可截断$$'
        self.document(text);self.k.sync_course('数学')
        status=self.k.status('数学')
        self.assertGreater(status['current_model_vectors_count'],0)
        self.assertLess(status['current_model_vectors_count'],status['active_chunks_count'])
        self.assertIn('OVERSIZE完整公式不可截断',self.k.search('数学','OVERSIZE')[0]['text'])
        source=self.k.store.list_sources('数学')[0]
        self.assertIn('超过模型输入预算',source['error'])
        self.assertEqual(self.k.sync_course('数学')['sources_updated'],0)

    def test_recording_without_scribbles_does_not_create_failed_source(self):
        self.records.create_record('lecture','数学','原课堂',[{'id':1,'start':0,'end':20,'raw':'数值孔径NA'}])
        self.k.sync_course('数学')
        sources=self.k.store.list_sources('数学',active_only=False)
        self.assertTrue(sources)
        self.assertFalse(any(s['source_id'].endswith(('_scribbles','_enhanced_notes')) for s in sources))

    async def test_shutdown_index_recovers_instead_of_becoming_user_cancelled(self):
        import threading,time
        started=threading.Event()
        def blocked(category,stop,progress_cb):
            started.set()
            while not stop():time.sleep(.005)
            return {}
        self.k.job_manager.sync_fn=blocked
        ident=self.k.schedule('数学')
        for _ in range(100):
            if started.is_set():break
            await asyncio.sleep(.005)
        await self.k.close()
        self.assertEqual(self.k.store.get_job(ident)['status'],'interrupted')
        replacement=KnowledgeService(self.settings,self.records,self.files,embedder=self.model)
        await replacement.start();self.addAsyncCleanup(replacement.close)
        for _ in range(100):
            if replacement.store.get_job(ident)['status']=='completed':break
            await asyncio.sleep(.005)
        self.assertEqual(replacement.store.get_job(ident)['status'],'completed')

    async def test_shutdown_debounce_and_extended_outage_remain_recoverable(self):
        record=SimpleNamespace(id='outage',directory=self.root/'outage',duration=20,metadata={'category':'数学'},segments=[])
        record.directory.mkdir();store=ClassroomNoteStore(record)
        note=await store.add_note('等网络恢复')
        service=ClassroomIntelligenceService(SimpleNamespace(find=lambda _:record),None)
        await service.enqueue_enhancement(record,note['id'],debounce_seconds=100)
        await service.close()
        self.assertEqual((await store.get_note(note['id']))['status'],'pending')
        from qwen_asr_app.classroom_intelligence.store import read_notes_doc,save_notes_doc
        doc=read_notes_doc(record);doc['notes'][0]['job']['retry_count']=10;save_notes_doc(record,doc)
        await store.recover_interrupted_notes()
        recovered=await store.get_note(note['id'])
        self.assertTrue(recovered['job']['retryable'])
        self.assertLessEqual(recovered['job']['backoff_seconds'],300)
        from qwen_asr_app.agy_cli import _error_kind
        self.assertEqual(_error_kind('Eligibility check failed: UNAVAILABLE (code 503): The service is currently unavailable.'),('provider_unavailable',True))

    def test_course_terminology_reuses_profile_and_invalidates_without_file_copy(self):
        self.settings.category_profiles={'数学':{'terminology':['数值孔径（NA）','离散傅里叶变换（DFT）']}}
        self.k.sync_course('数学')
        hit=self.k.search('数学','DFT')[0]
        self.assertEqual(hit['kind'],'专业术语')
        self.assertIn('settings=terminology',self.k.store.get_source(hit['source_id'])['open_url'])
        self.settings.category_profiles['数学']['terminology']=['数值孔径（NA）']
        self.assertTrue(self.k.validate_evidence([hit])[0]['stale'])
        self.assertFalse(self.k.search('数学','DFT'))

    def test_refined_transcript_retains_original_segment_mapping(self):
        from qwen_asr_app.knowledge.chunking import chunk_transcript_segments
        chunks=chunk_transcript_segments([{'text':'公式说明','segment_ids':[17,18],'start':12,'end':30},
                                         {'text':'条件说明','segment_ids':[18,19],'start':30,'end':40}])
        self.assertEqual(chunks[0].segment_ids,[17,18,19])
        self.assertEqual((chunks[0].start,chunks[0].end),(12,40))

    def test_unknown_named_identifier_requires_evidence_instead_of_guessing(self):
        from qwen_asr_app.classroom_chat import build_question,DEFAULT_PROVIDER,DEFAULT_CONTEXT
        record=SimpleNamespace(id='no-answer',duration=20,metadata={'category':'数学'},segments=[{'id':1,'start':0,'end':20,'raw':'今天讲数值孔径。'}])
        _,_,info=build_question(record,{'turns':[]},'ZXQ791 冷原子的温度是多少？',
            {'provider':DEFAULT_PROVIDER,'context':DEFAULT_CONTEXT},scope='lesson')
        self.assertTrue(info['clarification_required'])
        self.assertIn('未找到',info['clarification_message'])

    async def test_selected_material_is_shared_by_question_and_enhancement_context(self):
        from qwen_asr_app.classroom_chat import build_question,DEFAULT_PROVIDER,DEFAULT_CONTEXT
        from qwen_asr_app.classroom_intelligence.store import read_notes_doc,save_notes_doc
        self.document('公式 $x=1$，其中x是归一化后的输入量。');self.k.sync_course('数学')
        hit=self.k.search('数学','公式')[0]
        record=SimpleNamespace(id='manual-context',directory=self.root/'manual-context',duration=20,metadata={'category':'数学'},segments=[{'id':1,'start':0,'end':20,'raw':'接下来看这个概念。'}])
        record.directory.mkdir();doc=read_notes_doc(record)
        doc['source_hint']={'source_id':hit['source_id'],'revision':hit['revision'],'page':None};save_notes_doc(record,doc)
        refs=self.k.classroom_evidence(record,'这个公式是什么意思？')
        self.assertTrue(refs[0]['manual_context'])
        _,payload,info=build_question(record,{'turns':[]},'这个公式是什么意思？',{'provider':DEFAULT_PROVIDER,'context':DEFAULT_CONTEXT},knowledge=self.k)
        self.assertFalse(info.get('clarification_required',False))
        self.assertTrue(any(e.get('manual_context') for e in payload['evidence']))

    async def test_saved_note_sources_survive_summary_retrieval_and_stale_ones_are_dropped(self):
        from qwen_asr_app.transcription_assist.service import attach_accepted_evidence
        from qwen_asr_app.classroom_intelligence.store import read_notes_doc,save_notes_doc
        path=self.document('数值孔径NA=0.20。');self.k.sync_course('数学')
        hit=self.k.search('数学','NA')[0];hit['type']='material'
        record=SimpleNamespace(id='postclass-source',directory=self.root/'postclass-source',duration=20,metadata={'category':'数学'},segments=[])
        record.directory.mkdir();doc=read_notes_doc(record)
        doc['notes']=[{'content':'已接受的笔记','evidence':[hit]}];save_notes_doc(record,doc)
        with patch('qwen_asr_app.transcription_assist.service._knowledge',self.k):
            attached=await attach_accepted_evidence(record,{'_knowledge_evidence':[]})
            self.assertEqual(attached['_knowledge_evidence'][0]['citation_id'],hit['citation_id'])
            path.write_text('已更新到NA=0.24。')
            stale=await attach_accepted_evidence(record,{'_knowledge_evidence':[]})
            self.assertEqual(stale['_knowledge_evidence'],[])

    def test_classroom_runtime_defaults_local_without_rewriting_original_windows_preference(self):
        import os
        from qwen_asr_app.inference_providers import InferenceSettings,DEFAULTS
        legacy=self.root/'inference-providers.json'
        legacy.write_text(json.dumps(dict(DEFAULTS,asr_provider='remote_only',mineru_provider='remote_only')))
        before=legacy.read_bytes()
        with patch.dict(os.environ,{'QWEN_ASR_CLASSROOM_EDITION':'1'}),patch('qwen_asr_app.inference_providers.PRIVATE_ROOT',self.root):
            modern=InferenceSettings()
            self.assertEqual(modern.for_task('asr')['asr_provider'],'local')
            self.assertEqual(modern.for_task('mineru')['mineru_provider'],'local')
            self.assertEqual(legacy.read_bytes(),before)
            self.assertNotEqual(modern.path,legacy)

    def test_correction_basis_keeps_input_version_and_does_not_leak_credentials(self):
        from qwen_asr_app.transcription_assist.validation import correction_provenance
        config={'ai':{'api_key':'secret-test'},'_knowledge_evidence':[{'citation_id':'K1','source_id':'source','revision':'v1','content_hash':'hash','open_url':'/source'}]}
        evidence=correction_provenance('坡印廷矢量','波印廷矢量',config,[17],model='tested-model')
        self.assertEqual(evidence['before'],'坡印廷矢量')
        self.assertEqual(evidence['segment_ids'],[17])
        self.assertEqual(evidence['sources'][0]['revision'],'v1')
        self.assertNotIn('secret-test',json.dumps(evidence))
        self.assertIn('未逐项确认',evidence['support_status'])


if __name__=='__main__':unittest.main()
