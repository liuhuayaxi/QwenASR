from tests import isolation  # noqa: F401
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import app
from qwen_asr_app.ai import AIRequestError
from qwen_asr_app.note_content import transcript_blocks
from qwen_asr_app.transcription_assist import verification as verify
from qwen_asr_app.transcription_assist.routes import active_candidates
from tests.test_live_recovery import live_record, store_for

CONFIG={'ai':{'correction_provider':{'context_window':131072,'max_output_tokens':8000}}}


class AutomaticVerificationTests(unittest.IsolatedAsyncioTestCase):
    def record(self, directory, global_layer=False):
        record=live_record(Path(directory));record.metadata['status']='completed'
        record.segments=[{'id':i,'start':i*10,'end':i*10+9,'raw':f'本次数值为{i}，不是九。',
            'corrected':f'本次数值为{i}，不是九。'} for i in range(1,4)]
        holders=[]
        for segment in record.segments:
            holder=copy.deepcopy(segment);holder['segment_ids']=[segment['id']]
            holder['correction_review']={'before':segment['raw'],'after':f'本次数值为{segment["id"]}，不是9。',
                'risks':['数字或单位变化'],'evidence':[]}
            holders.append(holder)
        if global_layer:record.metadata['ai_global_rewrite']={'complete':True,'parts':[{'blocks':holders}]}
        else:record.metadata['ai_reviews']=holders
        record.save_all();return record

    def reply(self, payload):
        return {'decisions':[{'candidate_id':c['candidate_id'],'decision':['accept','keep','uncertain'][i%3],
            'reason':'对照原文后的逐项判断','evidence_ids':['S'+str(c['segment_ids'][0])]} for i,c in enumerate(payload['candidates'])]}

    async def run_verified(self,record,**kwargs):
        async def upstream(**request):return self.reply(request['payload'])
        async def evidence(r,c,t,**k):return c
        with patch.object(verify,'request_ai_json',side_effect=upstream), \
             patch('qwen_asr_app.transcription_assist.service.evidence_config',side_effect=evidence):
            return await verify.verify_corrections(record,CONFIG,**kwargs)

    def test_single_pass_finalization_preserves_raw_and_journals_proposals_once(self):
        from qwen_asr_app.refinement import finalize_corrections
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory,True)
            original=copy.deepcopy(record.segments)
            record.metadata['correction_verification']={'status':'interrupted','completed':0,'total':3}
            self.assertEqual(finalize_corrections(record),{'adopted':3})
            self.assertEqual(record.segments,original)
            self.assertFalse(active_candidates(record))
            self.assertNotIn('correction_verification',record.metadata)
            self.assertTrue(all('ai_verification' not in b for b in transcript_blocks(record)))
            files={p.name:p.read_bytes() for p in (record.directory/'.correction-finalizations').glob('*.json')}
            self.assertEqual(len(files),4)
            self.assertEqual(finalize_corrections(record),{'adopted':0})
            self.assertEqual(files,{p.name:p.read_bytes() for p in (record.directory/'.correction-finalizations').glob('*.json')})

    async def test_review_enabled_notes_use_full_pipeline_without_transcript_review(self):
        from qwen_asr_app.batch_ai import build_note_summary
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory);order=[]
            async def write(r,c):
                self.assertFalse(active_candidates(r))
                order.append('full')
                r.metadata['ai_note_summary']={'summary':'已保存的笔记正文','pdf_ready':True}
                r.save_metadata()
                return r.metadata['ai_note_summary']
            config={'note_review_enabled':True,'ai':{'summary_provider':{'transport':'agy_cli','cli_model':'gemini-3.8-flash-high'}}}
            with patch('qwen_asr_app.review_notes.generate',side_effect=write), \
                    patch('qwen_asr_app.batch_ai._section_page_links',new=AsyncMock()), \
                    patch.object(verify,'request_ai_json',new=AsyncMock()) as transcript_review:
                result=await build_note_summary(record,config)
            self.assertEqual(order,['full'])
            self.assertEqual(result['pdf_ready'],True)
            transcript_review.assert_not_awaited()

    async def test_independent_decisions_keep_segments_and_acoustic_source_and_resolve_uncertainty(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory,True);original=copy.deepcopy(record.segments)
            result=await self.run_verified(record)
            self.assertEqual(result,{'checked':3,'uncertain':1});self.assertEqual(record.segments,original)
            blocks=transcript_blocks(record)
            self.assertEqual([b['segment_ids'] for b in blocks],[[1],[2],[3]])
            self.assertTrue(all(b['refined'] for b in blocks));self.assertFalse(active_candidates(record))
            self.assertEqual(blocks[0]['text'],'本次数值为1，不是9。')
            self.assertEqual(blocks[1]['text'],original[1]['raw'])
            self.assertEqual(blocks[2]['ai_verification']['decision'],'uncertain')
            self.assertEqual(record.metadata['ai_global_rewrite']['text'],'\n\n'.join(b['text'] for b in blocks))
            with patch.object(verify,'request_ai_json',new=AsyncMock()) as request:
                await verify.verify_corrections(record,CONFIG);request.assert_not_awaited()

    async def test_missing_duplicate_reordered_or_fabricated_support_never_mutates_a_batch(self):
        for damage in ('missing','duplicate','reorder','fake_source','no_source','empty_reason'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as directory:
                record=self.record(directory);before=copy.deepcopy(record.metadata['ai_reviews'])
                async def upstream(**request):
                    result=self.reply(request['payload']);rows=result['decisions']
                    if damage=='missing':rows.pop()
                    elif damage=='duplicate':rows[1]=copy.deepcopy(rows[0])
                    elif damage=='reorder':rows.reverse()
                    elif damage=='fake_source':rows[0]['evidence_ids']=['Kmadeup']
                    elif damage=='no_source':rows[0]['evidence_ids']=[]
                    else:rows[0]['reason']=''
                    return result
                with patch.object(verify,'request_ai_json',side_effect=upstream):
                    with self.assertRaises(AIRequestError):await verify.verify_corrections(record,CONFIG)
                self.assertEqual(record.metadata['ai_reviews'],before)

    async def test_changed_raw_or_proposal_during_request_is_not_overwritten(self):
        for raw in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                record=self.record(directory)
                async def upstream(**request):
                    if raw:record.segments[0]['raw']='人工更正了原始内容'
                    else:record.metadata['ai_reviews'][0]['corrected']='人工修改'
                    return self.reply(request['payload'])
                with patch.object(verify,'request_ai_json',side_effect=upstream):
                    with self.assertRaisesRegex(AIRequestError,'来源已更新'):await verify.verify_corrections(record,CONFIG)
                self.assertEqual(len(active_candidates(record)),3)

    async def test_crash_after_receipt_before_save_recovers_without_repeating_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            with patch.object(verify,'_apply',side_effect=OSError('simulated disk failure')):
                with self.assertRaises(OSError):await self.run_verified(record)
            reloaded=type(record).load(record.directory)
            with patch.object(verify,'request_ai_json',new=AsyncMock()) as request:
                await verify.verify_corrections(reloaded,CONFIG)
                request.assert_not_awaited()
            self.assertFalse(active_candidates(reloaded))
            self.assertEqual(len(list((record.directory/'.correction-verifications').glob('[0-9a-f]'*64+'.json'))),3)

    async def test_changed_source_rejects_crash_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            with patch.object(verify,'_apply',side_effect=OSError('crash')):
                with self.assertRaises(OSError):await self.run_verified(record)
            record.segments[0]['raw']='新的人工原文'
            with self.assertRaises(AIRequestError):await self.run_verified(record)
            self.assertEqual(len(active_candidates(record)),3)

    async def test_transport_failure_and_cancellation_retain_all_proposals(self):
        for problem in (AIRequestError('network',kind='network_transport'),asyncio.CancelledError()):
            with tempfile.TemporaryDirectory() as directory:
                record=self.record(directory)
                with patch.object(verify,'request_ai_json',new=AsyncMock(side_effect=problem)):
                    with self.assertRaises(type(problem)):await verify.verify_corrections(record,CONFIG)
                self.assertEqual(len(active_candidates(record)),3)

    async def test_concurrent_callers_check_same_candidates_once(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            async def upstream(**request):
                await asyncio.sleep(.01);return self.reply(request['payload'])
            with patch.object(verify,'request_ai_json',side_effect=upstream) as request:
                await asyncio.gather(verify.verify_corrections(record,CONFIG),verify.verify_corrections(record,CONFIG))
            request.assert_awaited_once()

    async def test_output_budget_splits_whole_candidates_and_ignores_old_proposal_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            for holder in record.metadata['ai_reviews']:
                holder['correction_review']['evidence']=[{'citation_id':'old-revision','origin':'original','text':'旧课程版本'}]
            requests=[]
            async def upstream(**request):
                payload=request['payload'];requests.append(payload)
                self.assertFalse(payload['course_evidence'])
                return self.reply(payload)
            config={'ai':{'correction_provider':{'context_window':131072,'max_output_tokens':1456}}}
            with patch.object(verify,'request_ai_json',side_effect=upstream):await verify.verify_corrections(record,config)
            self.assertEqual([len(p['candidates']) for p in requests],[2,1])
            self.assertFalse(active_candidates(record))

    async def test_single_oversized_source_is_not_truncated_or_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            record.segments[0]['raw']='完整录音内容'*10000
            config={'ai':{'correction_provider':{'context_window':2048,'max_output_tokens':1024}}}
            with patch.object(verify,'request_ai_json',new=AsyncMock()) as request:
                with self.assertRaisesRegex(AIRequestError,'超出上下文'):await verify.verify_corrections(record,config)
                request.assert_not_awaited()
            self.assertEqual(len(active_candidates(record)),3)

    async def test_output_exhaustion_bisects_with_all_evidence_and_journals_each_child(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            attempts=[];accepted=[]
            evidence=[{'citation_id':'Kfull','origin':'original','text':'完整的原始课程证据'}]
            async def retrieve(r,c,t,**k):return {**c,'_knowledge_evidence':evidence}
            async def upstream(**request):
                payload=request['payload'];ids=[c['candidate_id'] for c in payload['candidates']]
                attempts.append(len(ids));self.assertEqual(payload['course_evidence'][0]['text'],evidence[0]['text'])
                if len(ids)>1:raise AIRequestError('output limit',kind='output_truncated')
                accepted.extend(ids)
                return self.reply(payload)
            with patch.object(verify,'request_ai_json',side_effect=upstream),patch('qwen_asr_app.transcription_assist.service.evidence_config',side_effect=retrieve):
                await verify.verify_corrections(record,CONFIG)
            self.assertEqual(attempts,[3,1,2,1,1])
            self.assertEqual(len(accepted),3);self.assertEqual(len(set(accepted)),3)
            self.assertFalse(active_candidates(record))
            self.assertEqual(len(list((record.directory/'.correction-verifications').glob('request-*.json'))),3)

    async def test_partial_split_success_survives_a_later_error_and_resumes_only_remaining(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory);accepted=[]
            async def upstream(**request):
                payload=request['payload']
                if len(payload['candidates'])>1:raise AIRequestError('limit',kind='output_truncated')
                if accepted:raise AIRequestError('auth',kind='provider_auth')
                accepted.append(payload['candidates'][0]['candidate_id']);return self.reply(payload)
            with patch.object(verify,'request_ai_json',side_effect=upstream):
                with self.assertRaises(AIRequestError):await verify.verify_corrections(record,CONFIG)
            self.assertEqual(len(active_candidates(record)),2)
            self.assertEqual(record.metadata['correction_verification']['status'],'failed')
            reloaded=type(record).load(record.directory)
            async def finish(**request):
                self.assertFalse(set(accepted)&{c['candidate_id'] for c in request['payload']['candidates']})
                return self.reply(request['payload'])
            with patch.object(verify,'request_ai_json',side_effect=finish):await verify.verify_corrections(reloaded,CONFIG)
            self.assertFalse(active_candidates(reloaded))

    async def test_cli_high_tries_the_whole_lesson_and_actual_output_limit_is_finite(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory);attempts=[]
            for i in range(4,21):
                segment=copy.deepcopy(record.segments[0]);segment.update(id=i,start=i*10,end=i*10+9)
                holder=copy.deepcopy(record.metadata['ai_reviews'][0]);holder.update(id=i,segment_ids=[i],start=i*10,end=i*10+9)
                record.segments.append(segment);record.metadata['ai_reviews'].append(holder)
            config={'ai':{'correction_provider':{'transport':'agy_cli','cli_model':'gemini-3.8-flash-high','context_window':256000}}}
            async def upstream(**request):
                attempts.append(len(request['payload']['candidates']))
                raise AIRequestError('output limit',kind='output_truncated')
            with patch.object(verify,'request_ai_json',side_effect=upstream):
                with self.assertRaises(AIRequestError):await verify.verify_corrections(record,config)
            self.assertEqual(attempts[0],20)
            self.assertEqual(attempts.count(1),20)
            self.assertEqual(len(attempts),39)
            self.assertEqual(len(active_candidates(record)),20)

    async def test_cli_full_lesson_uses_one_request_and_preserves_every_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            for i in range(4,54):
                segment=copy.deepcopy(record.segments[0]);segment.update(id=i,start=i*10,end=i*10+9)
                holder=copy.deepcopy(record.metadata['ai_reviews'][0]);holder.update(id=i,segment_ids=[i],start=i*10,end=i*10+9)
                record.segments.append(segment);record.metadata['ai_reviews'].append(holder)
            config={'ai':{'correction_provider':{'transport':'agy_cli','cli_model':'gemini-3.8-flash-high','context_window':1048576,'concurrency':6}}}
            async def upstream(**request):return self.reply(request['payload'])
            with patch.object(verify,'request_ai_json',side_effect=upstream) as request:
                result=await verify.verify_corrections(record,config)
            request.assert_awaited_once()
            self.assertEqual(len(request.call_args.kwargs['payload']['candidates']),53)
            self.assertEqual(result['checked'],53)
            self.assertFalse(active_candidates(record))

    async def test_cli_required_split_runs_independent_children_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory);active=peak=0
            config={'ai':{'correction_provider':{'transport':'agy_cli','cli_model':'gemini-3.8-flash-high','context_window':1048576,'concurrency':2}}}
            async def upstream(**request):
                nonlocal active,peak
                if len(request['payload']['candidates'])==3:
                    raise AIRequestError('actual output limit',kind='output_truncated')
                active+=1;peak=max(peak,active)
                await asyncio.sleep(.01)
                active-=1
                return self.reply(request['payload'])
            with patch.object(verify,'request_ai_json',side_effect=upstream):
                await verify.verify_corrections(record,config)
            self.assertEqual(peak,2)
            self.assertFalse(active_candidates(record))

    async def test_completed_refinement_installs_saved_corrections_without_another_ai_call(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            async def upstream(**request):return self.reply(request['payload'])
            with patch.object(verify,'request_ai_json',side_effect=upstream) as request, \
                 patch.object(app,'polish_timestamped_segments_resilient',new=AsyncMock()) as rewrite:
                await app._run_refinement_job_unlocked(record,CONFIG)
            request.assert_not_awaited();rewrite.assert_not_awaited()
            self.assertTrue(all(block['text'].endswith('不是9。') for block in transcript_blocks(record)))
            self.assertFalse(active_candidates(record))

    async def test_startup_queues_unchecked_completed_records_and_respects_pause_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            for status in ('completed','paused','cancelled'):
                record.metadata['ai_jobs']={'refinement':{'status':status}}
                record.save_all()
                with patch.object(app,'recording_store',store_for(record)), \
                     patch.object(app,'ai_task_action',new=AsyncMock()) as action:
                    await app.resume_correction_verifications()
                self.assertEqual(action.await_count,1 if status=='completed' else 0)

    def test_paused_and_failed_candidate_tasks_keep_their_real_status(self):
        with tempfile.TemporaryDirectory() as directory:
            record=self.record(directory)
            for status in ('paused','cancelled','failed'):
                record.metadata['ai_jobs']={'refinement':{'status':status,'error':'可恢复的失败' if status=='failed' else ''}}
                record.save_all()
                with patch.object(app,'recording_store',store_for(record)):
                    task=next(t for t in app.compact_ai_tasks() if t['type']=='refinement')
                self.assertEqual(task['status'],status)
