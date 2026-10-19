from tests import isolation
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
import app
from qwen_asr_app import ai
from qwen_asr_app.ai_limits import agy_account_gate, shared_gate
from qwen_asr_app.storage import RecordingRecord
from qwen_asr_app.note_content import compose_push
from qwen_asr_app.library_query import listing
from qwen_asr_app.library_search import LibrarySearch


class UnknownCreateTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_response_no_id_and_cancel_never_recreate_on_retry(self):
        for failure in ('timeout','no_id','cancel'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);record=RecordingRecord(root,{'id':'synthetic-note','title':'合成笔记','category':'合成课程','status':'completed',
                    'started_at':'2026-10-13T08:00:00+08:00','ai_note_summary':{'summary':'合成完整笔记'}})
                record.segments=[{'id':1,'start':0,'end':1,'raw':'合成逐字稿'}];record.save_all()
                calls=[]
                async def remote(name,args):
                    calls.append(name)
                    if name=='note_create':
                        durable=json.loads(record.meta_path.read_text())['ideashell_sync']['create_operation']
                        self.assertEqual(durable['status'],'sending')
                        self.assertTrue(durable['content_digest'])
                        if calls.count('note_create')>1:return {'note_id':'b'*32}
                        if failure=='timeout':raise TimeoutError('synthetic response lost')
                        if failure=='cancel':raise asyncio.CancelledError()
                        return {}
                    if name=='note_detail':return {'content':compose_push(record)['content']}
                    return {}
                connection=SimpleNamespace(server_url='https://synthetic.invalid',list_tools=AsyncMock(return_value={'note_create':{}}),call_tool=remote)
                with patch.object(app.recording_store,'find',side_effect=lambda _:record), \
                     patch.object(app,'ideashell_recording_audio_parts',AsyncMock(return_value=[])), \
                     patch.object(app.ideashell_manager,'connection',return_value=connection), \
                     patch.object(app,'ensure_ideashell_course_folder',AsyncMock(return_value='c'*32)):
                    with self.assertRaises((app.HTTPException,asyncio.CancelledError)):
                        await app.push_recording_summary_to_ideashell(record.id,{})
                    record=RecordingRecord.load(root) # service restart / fresh in-memory object
                    self.assertEqual(record.metadata['ideashell_sync']['create_operation']['status'],'unknown')
                    with self.assertRaises(app.HTTPException) as error:
                        await app.push_recording_summary_to_ideashell(record.id,{})
                    self.assertEqual(error.exception.status_code,409)
                    self.assertEqual(calls.count('note_create'),1)
                    # Explicit read-only reconciliation restores the link.
                    await app.reconcile_ideashell_create(record.id,{'note_id':'a'*32})
                    self.assertEqual(record.metadata['ideashell_sync']['note_id'],'a'*32)
                    await app.push_recording_summary_to_ideashell(record.id,{})
                    self.assertEqual(calls.count('note_create'),1)
                    self.assertIn('note_update',calls)
                    await app.push_recording_summary_to_ideashell(record.id,{'create_new':True})
                    self.assertEqual(calls.count('note_create'),2)
                    self.assertTrue(record.metadata['ideashell_sync']['create_history'])

    async def test_mismatching_reconciliation_keeps_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            record=RecordingRecord(Path(tmp),{'id':'synthetic-note','ideashell_sync':{'create_operation':{
                'status':'sending','server_url':'https://synthetic.invalid','folder_id':'c'*32,'reconcile_digest':'different'}}})
            record.save_metadata()
            connection=SimpleNamespace(call_tool=AsyncMock(return_value={'content':'changed remotely'}))
            with patch.object(app.recording_store,'find',side_effect=lambda _:record),patch.object(app.ideashell_manager,'connection',return_value=connection):
                with self.assertRaises(app.HTTPException) as error:
                    await app.reconcile_ideashell_create(record.id,{'note_id':'a'*32})
                self.assertEqual(error.exception.status_code,409)
                self.assertFalse(record.metadata['ideashell_sync'].get('note_id'))


class AGYTotalTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_limits_failures_and_waiting_cancellation(self):
        active=peak=0; per_mode={};peaks={}; release=asyncio.Event()
        async def work(**kw):
            nonlocal active,peak
            mode=kw['mode']; active+=1;peak=max(active,peak)
            per_mode[mode]=per_mode.get(mode,0)+1;peaks[mode]=max(peaks.get(mode,0),per_mode[mode])
            try:
                await release.wait()
                if kw['payload'].get('fail'):raise RuntimeError('synthetic failure')
                return 'done'
            finally:active-=1;per_mode[mode]-=1
        config={'ai':{'transport':'agy_cli','model':'fake','concurrency':2,
                      'summary_provider':{'transport':'agy_cli','model':'fake','concurrency':3}}}
        def request(mode,fail=False):return asyncio.create_task(ai.request_ai_text(instructions='synthetic',payload={'fail':fail},config=config,mode=mode,max_output_tokens=1))
        with patch.dict(os.environ,{'QWEN_ASR_AI_TOTAL_CONCURRENCY':'4'}),patch.object(ai,'_request_ai_text_unlimited',work):
            tasks=[request(mode,i==0) for mode in ('sentence','summary') for i in range(6)]
            await asyncio.sleep(.03)
            self.assertEqual(peak,4)
            tasks[-1].cancel();await asyncio.gather(tasks[-1],return_exceptions=True)
            gate=agy_account_gate(); self.assertEqual(gate.active,4)
            with patch.dict(os.environ,{'QWEN_ASR_AI_TOTAL_CONCURRENCY':'1'}):
                self.assertIs(agy_account_gate(),gate)
                self.assertEqual(gate.limit,1)
                self.assertEqual(gate.active,4) # admitted work completes; no new admissions
                release.set();results=await asyncio.gather(*tasks,return_exceptions=True)
                self.assertTrue(any(isinstance(value,RuntimeError) for value in results))
                self.assertEqual(gate.active,0)
                self.assertEqual(await request('sentence'),'done')
                self.assertEqual(gate.active,0)
            self.assertLessEqual(peaks['sentence'],2);self.assertLessEqual(peaks['summary'],3)

    async def test_function_reconfiguration_reuses_pool(self):
        gate=shared_gate(('synthetic','function'),3)
        await gate.__aenter__();await gate.__aenter__()
        self.assertIs(shared_gate(('synthetic','function'),1),gate)
        waiting=asyncio.create_task(gate.__aenter__());await asyncio.sleep(0)
        self.assertFalse(waiting.done());waiting.cancel();await asyncio.gather(waiting,return_exceptions=True)
        await gate.__aexit__();await gate.__aexit__()
        self.assertEqual(gate.active,0)
        async with gate:self.assertEqual(gate.active,1)


class LibraryQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_size_pagination_search_detail_and_etag(self):
        with tempfile.TemporaryDirectory() as tmp:
            records=[]
            for i in range(100):
                record=RecordingRecord(Path(tmp)/str(i),{'id':f'synthetic-{i:03d}','title':f'课堂 {i}',
                    'category':'光学' if i%2 else '物理','status':'completed','started_at':'2026-10-13T08:00:00+08:00',
                    'ai_note_summary':{'summary':'中'*20000+'唯一查询词','abstract':'合成摘要'},
                    'ai_reviews':[{'corrected':'过程'*20000}], 'pending_segments':{'1':{'large':'大'*20000}}})
                record.segments=[{'id':1,'start':0,'end':1,'raw':'逐字稿'*20000}];records.append(record)
            payload=listing(records)
            size=len(json.dumps(payload,ensure_ascii=False).encode())
            self.assertLess(size,180000)
            for item in payload['records']:
                self.assertNotIn('summary',item['ai_note_summary']);self.assertTrue(item['has_note'])
                self.assertNotIn('segments',item);self.assertNotIn('pending_segments',item);self.assertNotIn('ai_reviews',item)
            page=listing(records,'光学',offset=20,limit=20)
            self.assertEqual(page['total'],50);self.assertEqual(page['next_offset'],40)
            self.assertEqual(page['course_counts']['光学']['notes'],50)
            self.assertEqual(page['calendar']['2026-10-13']['count'],50)
            self.assertTrue(all(item['category']=='光学' for item in page['records']))
            self.assertEqual(len(LibrarySearch().search(records,'唯一查询词','物理')['matches']),30)
            self.assertEqual(LibrarySearch().search(records,'唯一查询词','物理')['total'],50)
            with patch.object(app.recording_store,'list_records',return_value=records),patch.object(app.recording_store,'iter_records',side_effect=lambda:iter(records)),patch.object(app.recording_store,'iter_list_items',side_effect=lambda:(record.list_item() for record in records)),patch.object(app.recording_store,'find',return_value=records[0]),patch.object(app,'discover_courses'):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://test') as client:
                    for old_page in ('/notes','/static/notes.html'):
                        redirect=await client.get(old_page+'?category=optics')
                        self.assertEqual(redirect.status_code,307)
                        self.assertEqual(redirect.headers['location'],'/course-files?category=optics')
                    response=await client.get('/api/library?category=光学&limit=20')
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.json()['total'],50)
                    cached=await client.get('/api/library?category=光学&limit=20',headers={'If-None-Match':response.headers['etag']})
                    self.assertEqual(cached.status_code,304);self.assertEqual(cached.content,b'')
                    detail=await client.get('/api/recordings/synthetic-000')
                    self.assertIn('唯一查询词',detail.json()['session']['ai_note_summary']['summary'])
                    records[1].metadata['title']='新的标题'
                    changed=await client.get('/api/library?category=光学&limit=20',headers={'If-None-Match':response.headers['etag']})
                    self.assertEqual(changed.status_code,200)
