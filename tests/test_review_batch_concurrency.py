"""One final PDF, bounded reviewers, durable completion, and owned cleanup."""
from tests import isolation  # noqa: F401

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qwen_asr_app.review_notes import visual_review


def candidate(folder, pages=12):
    import fitz
    pdf=folder/'candidate.pdf'
    with fitz.open() as document:
        for number in range(1,pages+1):
            document.new_page().insert_text((50,50),f'Actual synthetic review page {number}')
        document.save(pdf)
    return pdf,{'pages':pages,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'renderer':'test'}


def outcome(prompt, kwargs):
    payload=json.loads(prompt.rsplit('\n',1)[-1]);numbers=payload['pages']
    assert len(kwargs['require_images'])<=4
    assert all(path.is_file() and path.read_bytes().startswith(b'\x89PNG') for path in kwargs['require_images'])
    return numbers,(json.dumps({'status':'passed','issues':[],'viewed_pages':numbers}),
        {'session_id':f'reviewer-{numbers[0]}','viewed_files':[str(path) for path in kwargs['require_images']]})


class ReviewBatchConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_pdfs_are_serial_while_one_pdf_keeps_two_reviewers(self):
        with tempfile.TemporaryDirectory() as raw:
            first=Path(raw)/'first';second=Path(raw)/'second'
            first.mkdir();second.mkdir()
            first_pdf,first_export=candidate(first,8)
            second_pdf,second_export=candidate(second,4)
            both_entered=asyncio.Event();release_first=asyncio.Event()
            active={};maximum=0;starts=[]
            async def model(prompt,folder,provider,**kwargs):
                nonlocal maximum
                numbers,result=outcome(prompt,kwargs)
                active[folder]=active.get(folder,0)+1
                maximum=max(maximum,sum(active.values()))
                starts.append((folder,numbers[0]))
                self.assertEqual(sum(bool(count) for count in active.values()),1)
                try:
                    if folder==first:
                        if active[first]==2:both_entered.set()
                        await release_first.wait()
                    return result
                finally:active[folder]-=1
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                owner=asyncio.create_task(visual_review(first_pdf,first_export,first,{'concurrency':2},[],[]))
                await asyncio.wait_for(both_entered.wait(),2)
                queued=asyncio.create_task(visual_review(second_pdf,second_export,second,{'concurrency':2},[],[]))
                await asyncio.sleep(0)
                self.assertFalse(queued.done())
                # No page directory is created before this PDF owns the slot.
                self.assertFalse((second/'pages').exists())
                self.assertEqual(starts,[(first,1),(first,5)])
                release_first.set()
                reports=await asyncio.wait_for(asyncio.gather(owner,queued),2)
            self.assertEqual(maximum,2)
            self.assertEqual(starts,[(first,1),(first,5),(second,1)])
            self.assertEqual([report['status'] for report in reports],['passed','passed'])

    async def test_cancelling_waiting_pdf_leaves_owner_and_its_reviewers_running(self):
        with tempfile.TemporaryDirectory() as raw:
            first=Path(raw)/'first';second=Path(raw)/'second'
            first.mkdir();second.mkdir()
            first_pdf,first_export=candidate(first,8)
            second_pdf,second_export=candidate(second,4)
            both_entered=asyncio.Event();release_first=asyncio.Event()
            entered=[];cancelled=[]
            async def model(prompt,folder,provider,**kwargs):
                numbers,result=outcome(prompt,kwargs)
                self.assertEqual(folder,first)
                entered.append(numbers[0])
                if len(entered)==2:both_entered.set()
                try:await release_first.wait();return result
                except asyncio.CancelledError:
                    cancelled.append(numbers[0]);raise
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                owner=asyncio.create_task(visual_review(first_pdf,first_export,first,{'concurrency':2},[],[]))
                await asyncio.wait_for(both_entered.wait(),2)
                queued=asyncio.create_task(visual_review(second_pdf,second_export,second,{'concurrency':2},[],[]))
                await asyncio.sleep(0);queued.cancel()
                with self.assertRaises(asyncio.CancelledError):await queued
                self.assertFalse(owner.done());self.assertEqual(cancelled,[])
                self.assertFalse((second/'pages').exists())
                release_first.set()
                report=await asyncio.wait_for(owner,2)
            self.assertEqual(report['status'],'passed')
            self.assertEqual(entered,[1,5]);self.assertEqual(cancelled,[])

    async def test_short_batch_reads_adjacent_solution_page_within_four_image_limit(self):
        from qwen_asr_app.review_notes import REVIEW_POLICY
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf,export=candidate(folder,4)
            (folder/'review.json').write_text(json.dumps({'pdf_sha256':export['pdf_sha256'],
                'policy':REVIEW_POLICY,'learning_scope_sha256':None,'status':'unreviewed',
                'viewed_pages':[],'issues':[],'reviewer_session_ids':[],'batches':[],'batch_size':2}))
            contexts=[]
            async def model(prompt,folder,provider,**kwargs):
                payload=json.loads(prompt.rsplit('\n',1)[-1]);contexts.append(payload['context_pages'])
                self.assertLessEqual(len(kwargs['require_images']),4)
                for context in payload['context_pages']:
                    self.assertIn(Path(context['path']),kwargs['require_images'])
                return outcome(prompt,kwargs)[1]
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                report=await visual_review(pdf,export,folder,{'concurrency':1},[],[])
            self.assertEqual([value[0]['page'] for value in contexts],[3,2])
            self.assertEqual([batch['context_pages'] for batch in report['batches']],[[3],[2]])
            self.assertEqual(report['viewed_pages'],[1,2,3,4])

    async def test_two_out_of_order_batches_persist_immediately_and_final_report_is_sorted(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf,export=candidate(folder)
            release_first=asyncio.Event();two_saved=asyncio.Event();active=0;maximum=0;starts=[];snapshots=[]
            async def model(prompt,folder,provider,**kwargs):
                nonlocal active,maximum
                numbers,result=outcome(prompt,kwargs);starts.append(numbers)
                active+=1;maximum=max(maximum,active)
                try:
                    if numbers[0]==1:await release_first.wait()
                    return result
                finally:active-=1
            def progress(done,total):
                saved=json.loads((folder/'review.json').read_text());snapshots.append(saved)
                if done==8:two_saved.set()
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                task=asyncio.create_task(visual_review(pdf,export,folder,{'concurrency':8},[],[],progress=progress))
                await asyncio.wait_for(two_saved.wait(),2)
                self.assertFalse(task.done());self.assertEqual(maximum,2)
                self.assertEqual(snapshots[-1]['viewed_pages'],list(range(5,13)))
                self.assertEqual([b['pages'][0] for b in snapshots[-1]['batches']],[5,9])
                release_first.set();report=await task
            self.assertEqual(starts,[list(range(1,5)),list(range(5,9)),list(range(9,13))])
            self.assertEqual([b['pages'][0] for b in report['batches']],[1,5,9])
            self.assertEqual(report['reviewer_session_ids'],['reviewer-1','reviewer-5','reviewer-9'])
            self.assertEqual(report['viewed_pages'],list(range(1,13)))
            self.assertEqual(report['status'],'passed');self.assertEqual(active,0)

    async def test_provider_limit_one_keeps_all_review_calls_serial(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf,export=candidate(folder,8);active=0;maximum=0
            async def model(prompt,folder,provider,**kwargs):
                nonlocal active,maximum
                _,result=outcome(prompt,kwargs);active+=1;maximum=max(maximum,active)
                try:await asyncio.sleep(0);return result
                finally:active-=1
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                report=await visual_review(pdf,export,folder,{'concurrency':1},[],[])
            self.assertEqual(maximum,1);self.assertEqual(report['status'],'passed')

    async def test_failure_drains_sibling_and_resume_skips_durable_success(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf,export=candidate(folder)
            release_failure=asyncio.Event();sibling_entered=asyncio.Event();sibling_cancelled=asyncio.Event();cleanup_release=asyncio.Event()
            active=0
            async def model(prompt,folder,provider,**kwargs):
                nonlocal active
                numbers,result=outcome(prompt,kwargs);active+=1
                try:
                    if numbers[0]==1:
                        await release_failure.wait();raise RuntimeError('synthetic review failure')
                    if numbers[0]==9:
                        sibling_entered.set()
                        try:await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            sibling_cancelled.set();await cleanup_release.wait();raise
                    return result
                finally:active-=1
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                task=asyncio.create_task(visual_review(pdf,export,folder,{'concurrency':2},[],[]))
                await asyncio.wait_for(sibling_entered.wait(),2);release_failure.set()
                await asyncio.wait_for(sibling_cancelled.wait(),2)
                self.assertFalse(task.done());self.assertEqual(active,1)
                saved=json.loads((folder/'review.json').read_text())
                self.assertEqual(saved['viewed_pages'],[5,6,7,8]);self.assertEqual(saved['status'],'unreviewed')
                cached=folder/'pages'/export['pdf_sha256']/'page-005.png';before=hashlib.sha256(cached.read_bytes()).hexdigest()
                cleanup_release.set()
                with self.assertRaisesRegex(RuntimeError,'synthetic review failure'):await task
            self.assertEqual(active,0);calls=[]
            async def resume(prompt,folder,provider,**kwargs):
                numbers,result=outcome(prompt,kwargs);calls.append(numbers);return result
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=resume):
                report=await visual_review(pdf,export,folder,{'concurrency':2},[],[])
            self.assertEqual(calls,[list(range(1,5)),list(range(9,13))])
            self.assertEqual(hashlib.sha256(cached.read_bytes()).hexdigest(),before)
            self.assertEqual(report['status'],'passed');self.assertEqual(report['viewed_pages'],list(range(1,13)))

    async def test_repeated_parent_cancellation_waits_for_both_owned_reviewers(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf,export=candidate(folder,8)
            waiting_folder=folder/'waiting';waiting_folder.mkdir()
            waiting_pdf,waiting_export=candidate(waiting_folder,4)
            both_entered=asyncio.Event();both_cancelled=asyncio.Event();cleanup_release=asyncio.Event()
            active=0;entered=[];cancelled=[];drained=[]
            async def model(prompt,folder,provider,**kwargs):
                nonlocal active
                numbers,result=outcome(prompt,kwargs)
                if folder==waiting_folder:
                    self.assertEqual(active,0)
                    return result
                entered.append(numbers[0]);active+=1
                if len(entered)==2:both_entered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(numbers[0])
                    if len(cancelled)==2:both_cancelled.set()
                    await cleanup_release.wait();drained.append(numbers[0]);raise
                finally:active-=1
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                task=asyncio.create_task(visual_review(pdf,export,folder,{'concurrency':2},[],[]))
                await asyncio.wait_for(both_entered.wait(),2);task.cancel()
                await asyncio.wait_for(both_cancelled.wait(),2)
                task.cancel();await asyncio.sleep(0);task.cancel();await asyncio.sleep(0)
                self.assertFalse(task.done());self.assertEqual(active,2);self.assertEqual(drained,[])
                waiting=asyncio.create_task(visual_review(waiting_pdf,waiting_export,waiting_folder,{'concurrency':2},[],[]))
                await asyncio.sleep(0)
                self.assertFalse(waiting.done());self.assertFalse((waiting_folder/'pages').exists())
                cleanup_release.set()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual((await asyncio.wait_for(waiting,2))['status'],'passed')
            self.assertEqual(sorted(cancelled),[1,5]);self.assertEqual(sorted(drained),[1,5]);self.assertEqual(active,0)


if __name__=='__main__':unittest.main()

class ReviewStageProgressTests(unittest.TestCase):
    def test_entering_review_does_not_reuse_writing_batch_or_other_pdf_counts(self):
        from qwen_asr_app.review_notes import review_start_progress
        export = {'pages': 38, 'pdf_sha256': 'a' * 64}
        self.assertEqual(review_start_progress({'completed_parts': 6, 'total_parts': 6}, export),
                         {'reviewed_pages': 0, 'review_total_pages': 38})
        self.assertEqual(review_start_progress({'pdf_sha256': 'b' * 64, 'reviewed_pages': 28,
                                               'review_total_pages': 28}, export)['reviewed_pages'], 0)

    def test_same_review_resumes_saved_pages_and_rejects_invalid_checkpoint(self):
        from qwen_asr_app.review_notes import review_start_progress
        export = {'pages': 38, 'pdf_sha256': 'a' * 64}
        task = {'pdf_sha256': export['pdf_sha256'], 'reviewed_pages': 8, 'review_total_pages': 38}
        self.assertEqual(review_start_progress(task, export)['reviewed_pages'], 8)
        for bad in [True, -1, 39, '8']:
            self.assertEqual(review_start_progress({**task, 'reviewed_pages': bad}, export)['reviewed_pages'], 0)

class ReviewRequestVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_page_batch_and_deadline_are_saved_while_waiting(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf,export=candidate(folder,4)
            async def model(prompt,folder,provider,**kwargs):
                self.assertEqual(provider['cli_timeout_seconds'],300)
                report=json.loads((folder/'review.json').read_text())
                running=list(report['active_batches'].values())
                self.assertEqual(running[0]['pages'],[1,2,3,4])
                self.assertEqual(running[0]['attempt'],1)
                return outcome(prompt,kwargs)[1]
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                report=await visual_review(pdf,export,folder,{'cli_timeout_seconds':1800},[],[])
            self.assertEqual(report['active_batches'],{})
            self.assertEqual(report['viewed_pages'],[1,2,3,4])

    async def test_request_timeout_is_bounded_and_completed_batches_survive_resume(self):
        from qwen_asr_app.agy_materials import MaterialSessionError
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf,export=candidate(folder,8);calls=[]
            async def model(prompt,folder,provider,**kwargs):
                numbers,result=outcome(prompt,kwargs);calls.append(numbers)
                if numbers[0]==5:raise TimeoutError('provider stalled')
                return result
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                with self.assertRaises(MaterialSessionError) as caught:
                    await visual_review(pdf,export,folder,{'concurrency':1},[],[])
            self.assertEqual(caught.exception.kind,'review_batch_exhausted')
            self.assertEqual(len(calls),4)
            report=json.loads((folder/'review.json').read_text())
            self.assertEqual(report['viewed_pages'],[1,2,3,4]);self.assertEqual(report['active_batches'],{})
            async def resume(prompt,folder,provider,**kwargs):
                self.assertEqual(outcome(prompt,kwargs)[0],[5,6,7,8]);return outcome(prompt,kwargs)[1]
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=resume):
                report=await visual_review(pdf,export,folder,{},[],[])
            self.assertEqual(report['status'],'passed')
