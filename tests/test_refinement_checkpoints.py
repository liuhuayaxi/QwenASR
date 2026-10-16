from tests import isolation
import asyncio,json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import app
from qwen_asr_app import ai
from qwen_asr_app.refinement import commit_blocks,restore_checkpoints,pending_ids,context_for,saved_progress
from tests.test_reader_pipeline import record_at

CONFIG={'_parallel_refinement':True,'ai':{'correction_provider':{'transport':'agy_cli',
    'cli_model':'gemini-3.8-flash-high','context_window':1048576,'concurrency':6}}}


class RefinementCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def record(self,path):
        record=record_at(Path(path),ai_note_summary={})
        record.metadata['ai_reviews']=[]
        record.segments=[{'id':i,'raw':f'完整课堂原文{i}','start':i-1,'end':i} for i in range(1,5)]
        record.save_all();return record

    async def test_successful_half_is_saved_if_second_half_fails_and_resume_only_requests_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            record=self.record(raw)
            async def upstream(segments,*args,**kwargs):
                ids=[s['id'] for s in segments]
                if ids==[1,2,3,4]:raise ai.AIRequestError('bad mapping',kind='invalid_segment_mapping')
                if ids==[3,4]:raise ai.AIRequestError('network',kind='network_transport')
                return [{'segment_ids':[s['id']],'corrected':'校对'+str(s['id'])} for s in segments]
            with patch('qwen_asr_app.ai.polish_timestamped_segments',side_effect=upstream):
                with self.assertRaises(ai.AIRequestError):await app._run_refinement_job_unlocked(record,CONFIG)
            self.assertEqual(pending_ids(record),{3,4})
            self.assertEqual(saved_progress(record)['saved_segments'],2)
            reloaded=type(record).load(record.directory)
            before=json.dumps(reloaded.metadata['ai_reviews'],ensure_ascii=False)
            async def finish(segments,*args,**kwargs):
                self.assertEqual([s['id'] for s in segments],[3,4])
                return [{'segment_ids':[s['id']],'corrected':'校对'+str(s['id'])} for s in segments]
            with patch.object(app,'polish_timestamped_segments_resilient',new=AsyncMock(side_effect=finish)) as call:
                await app._run_refinement_job_unlocked(reloaded,CONFIG)
                call.assert_awaited_once()
            self.assertEqual(reloaded.metadata['ai_reviews'][:2],json.loads(before))
            self.assertFalse(pending_ids(reloaded))

    async def test_cancel_after_valid_half_keeps_checkpoint_and_visible_text(self):
        with tempfile.TemporaryDirectory() as raw:
            record=self.record(raw);waiting=asyncio.Event()
            async def upstream(segments,*args,**kwargs):
                if len(segments)==4:raise ai.AIRequestError('bad mapping',kind='invalid_segment_mapping')
                if segments[0]['id']==3:
                    waiting.set();await asyncio.Event().wait()
                return [{'segment_ids':[s['id']],'corrected':'保存'+str(s['id'])} for s in segments]
            with patch('qwen_asr_app.ai.polish_timestamped_segments',side_effect=upstream):
                task=asyncio.create_task(app._run_refinement_job_unlocked(record,CONFIG))
                await asyncio.wait_for(waiting.wait(),1);task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(pending_ids(type(record).load(record.directory)),{3,4})

    def test_pending_scientific_review_is_saved_work_not_a_new_compute_target(self):
        with tempfile.TemporaryDirectory() as raw:
            record=self.record(raw)
            commit_blocks(record,[{'segment_ids':[1,2],'corrected':'保留原文',
                'correction_review':{'before':'保留原文','after':'数字候选','status':'pending'}}])
            self.assertEqual(pending_ids(record),{3,4})
            self.assertEqual(saved_progress(record)['review_blocks'],1)

    def test_journal_recovers_crash_before_install_but_never_overwrites_changed_source(self):
        with tempfile.TemporaryDirectory() as raw:
            record=self.record(raw)
            with patch('qwen_asr_app.refinement.install_blocks',side_effect=OSError('crash')):
                with self.assertRaises(OSError):commit_blocks(record,[{'segment_ids':[1],'corrected':'校对成功'}])
            restore_checkpoints(record);self.assertNotIn(1,pending_ids(record))
            record.metadata['ai_reviews']=[];record.segments[0]['raw']='人工修改原文'
            restore_checkpoints(record);self.assertIn(1,pending_ids(record))

    def test_segment_checkpoint_recovers_and_explicit_new_generation_does_not_restore_old_result(self):
        with tempfile.TemporaryDirectory() as raw:
            record=self.record(raw)
            commit_blocks(record,[{'segment_ids':[1],'corrected':'已保存校对'}],destination='segments')
            record.segments[0].pop('corrected')
            restore_checkpoints(record)
            self.assertEqual(record.segments[0]['corrected'],'已保存校对')
            record.segments[0].pop('corrected');record.metadata['refinement_epoch']='new-generation'
            restore_checkpoints(record)
            self.assertNotIn('corrected',record.segments[0])

    async def test_retry_preserves_successful_manual_reviews_and_cancelled_work_stays_resumable(self):
        with tempfile.TemporaryDirectory() as raw:
            record=self.record(raw)
            commit_blocks(record,[{'segment_ids':[1],'corrected':'成功结果'}])
            before=json.loads(json.dumps(record.metadata['ai_reviews']))
            with patch.object(app.recording_store,'find',return_value=record), \
                 patch.object(app,'_saved_ai_task_config',return_value=CONFIG), \
                 patch.object(app,'_start_aux_ai_job',return_value=True):
                result=await app.ai_task_action({'recording_id':record.id,'type':'refinement','action':'retry'})
            self.assertTrue(result['accepted'])
            self.assertEqual(record.metadata['ai_reviews'],before)
            record.metadata.setdefault('ai_jobs',{})['refinement']={'status':'cancelled','completed':1,'total':4}
            with patch.object(app.recording_store,'list_records',return_value=[record]):
                tasks=app.compact_ai_tasks()
            item=next(t for t in tasks if t['type']=='refinement')
            self.assertEqual(item['completed'],1);self.assertEqual(item['total'],4)
            self.assertIn('continue',item['actions'])

    async def test_normal_long_lecture_is_not_divided_by_concurrency_and_context_has_both_sides(self):
        with tempfile.TemporaryDirectory() as raw:
            record=self.record(raw)
            record.segments=[{'id':i,'raw':'课堂术语和完整解释。'*30,'start':i*60,'end':i*60+59} for i in range(1,61)]
            async def finish(segments,*args,**kwargs):
                return [{'segment_ids':[s['id']],'corrected':s['raw']} for s in segments]
            with patch.object(app,'polish_timestamped_segments_resilient',new=AsyncMock(side_effect=finish)) as call:
                await app._run_refinement_job_unlocked(record,CONFIG);call.assert_awaited_once()
            context=context_for(record,record.segments[25:35],1048576)
            self.assertIn('前文',context);self.assertIn('后文',context)
            self.assertGreater(len(context),2000)


if __name__=='__main__':unittest.main()
