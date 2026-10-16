from tests import isolation
import copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from qwen_asr_app.transcription_assist.routes import transcription_assist_router,active_candidates
from qwen_asr_app.note_content import transcript_blocks
from qwen_asr_app.storage import RecordingRecord


class CorrectionDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.record=RecordingRecord(Path(self.tmp.name),{'id':'correction-example','title':'课堂','category':'未分类',
            'status':'completed','sample_count':16000,'ai_reviews':[]})
        self.proposal={'before':'功率为40 mW','after':'功率为40 W','risks':['数字或单位变化'],'status':'待核对','evidence':[]}
        self.record.segments=[{'id':1,'start':0,'end':1,'raw':'功率为40 mW','corrected':'功率为40 mW','correction_review':copy.deepcopy(self.proposal)}]
        self.record.save_all()
        api=FastAPI();self.busy=False
        def guard(ident):
            if self.busy:raise HTTPException(409,'请等待当前任务保存后核对')
        api.include_router(transcription_assist_router(SimpleNamespace(find=lambda ident:self.record),guard))
        self.client=TestClient(api);self.addCleanup(self.client.close)
        self.url='/api/recordings/correction-example/correction-review'

    def test_accept_keep_and_edit_are_durable_and_preserve_original_audio_text(self):
        for decision,expected in [('accept','功率为40 W'),('keep','功率为40 mW'),('edit','人工校对为40毫瓦')]:
            with self.subTest(decision=decision):
                self.record.metadata['correction_decisions']=[]
                self.record.segments[0].update(corrected='功率为40 mW',correction_review=copy.deepcopy(self.proposal))
                item=self.client.get(self.url).json()['candidates'][0]
                payload={'candidate_id':item['candidate_id'],'decision':decision}
                if decision=='edit':payload['text']=expected
                response=self.client.post(self.url,json=payload)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['remaining'],0)
                loaded=RecordingRecord.load(self.record.directory)
                self.assertEqual(transcript_blocks(loaded)[0]['text'],expected)
                self.assertEqual(loaded.segments[0]['raw'],'功率为40 mW')
                self.assertTrue(transcript_blocks(loaded)[0]['refined'])
                self.assertTrue((loaded.directory/'.correction-decisions'/(item['candidate_id']+'.json')).is_file())

    def test_same_decision_can_be_retried_but_conflicting_or_stale_decision_cannot_overwrite(self):
        item=self.client.get(self.url).json()['candidates'][0]
        payload={'candidate_id':item['candidate_id'],'decision':'keep'}
        self.assertEqual(self.client.post(self.url,json=payload).status_code,200)
        self.assertTrue(self.client.post(self.url,json=payload).json()['reused'])
        self.assertEqual(self.client.post(self.url,json={**payload,'decision':'accept'}).status_code,409)
        self.assertEqual(len(self.record.metadata['correction_decisions']),1)
        self.record.segments[0]['correction_review']=copy.deepcopy(self.proposal)
        self.record.metadata['correction_decisions']=[]
        fresh=self.client.get(self.url).json()['candidates'][0]
        self.record.segments[0]['raw']='人工修改后的原文'
        self.assertEqual(self.client.post(self.url,json={'candidate_id':fresh['candidate_id'],'decision':'accept'}).status_code,409)

    def test_current_layer_is_counted_once_and_resolving_it_does_not_resurface_old_candidate(self):
        self.record.metadata['ai_reviews']=[{'id':'newer','segment_ids':[1],'start':0,'end':1,'raw':'功率为40 mW',
            'corrected':'功率为40 mW','correction_review':{**self.proposal,'after':'功率为40千瓦'}}]
        items=self.client.get(self.url).json()['candidates']
        self.assertEqual(len(items),1);self.assertEqual(items[0]['after'],'功率为40千瓦')
        response=self.client.post(self.url,json={'candidate_id':items[0]['candidate_id'],'decision':'keep'})
        self.assertEqual(response.json()['remaining'],0)
        self.assertEqual(active_candidates(self.record),[])

    def test_busy_writer_and_empty_manual_edit_are_rejected_without_mutation(self):
        item=self.client.get(self.url).json()['candidates'][0]
        before=copy.deepcopy(self.record.segments)
        self.busy=True
        response=self.client.post(self.url,json={'candidate_id':item['candidate_id'],'decision':'accept'})
        self.assertEqual(response.status_code,409)
        self.busy=False
        self.assertEqual(self.client.post(self.url,json={'candidate_id':item['candidate_id'],'decision':'edit','text':' '}).status_code,400)
        self.assertEqual(self.record.segments,before)

    def test_resolving_one_batch_member_keeps_each_timestamp_and_neighbor_proposal(self):
        self.record.segments.append({'id':2,'start':1,'end':2,'raw':'第二段原文','corrected':'第二段原文',
            'correction_review':{**self.proposal,'before':'第二段原文','after':'第二段建议'}})
        self.record.save_all()
        before=copy.deepcopy(self.record.segments[1])
        candidates=self.client.get(self.url).json()['candidates']
        self.assertEqual([item['segment_ids'] for item in candidates],[[1],[2]])
        result=self.client.post(self.url,json={'candidate_id':candidates[0]['candidate_id'],'decision':'accept'})
        self.assertEqual(result.status_code,200)
        blocks=result.json()['session']['refined_transcript']
        self.assertEqual([(b['segment_ids'],b['start'],b['end']) for b in blocks],[([1],0,1),([2],1,2)])
        self.assertEqual(blocks[0]['text'],'功率为40 W')
        self.assertEqual(self.record.segments[1],before)
        self.assertEqual(result.json()['remaining'],1)

    def test_global_batch_review_updates_only_one_nested_segment_and_its_export_text(self):
        self.record.segments.append({'id':2,'start':1,'end':2,'raw':'第二段','corrected':'第二段'})
        self.record.metadata['ai_global_rewrite']={'complete':True,'text':'旧文本','parts':[{'segment_ids':[1,2],
            'corrected':'旧文本','blocks':[{'segment_ids':[1],'start':0,'end':1,'corrected':'功率为40 mW','correction_review':copy.deepcopy(self.proposal)},
                {'segment_ids':[2],'start':1,'end':2,'corrected':'第二段精修'}]}]}
        self.record.save_all();candidate=self.client.get(self.url).json()['candidates'][0]
        response=self.client.post(self.url,json={'candidate_id':candidate['candidate_id'],'decision':'accept'})
        self.assertEqual(response.status_code,200,response.text)
        loaded=RecordingRecord.load(self.record.directory)
        self.assertEqual([b['text'] for b in transcript_blocks(loaded)],['功率为40 W','第二段精修'])
        self.assertEqual(loaded.metadata['ai_global_rewrite']['text'],'功率为40 W\n\n第二段精修')
