from tests import isolation
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import httpx
import numpy as np
import soundfile as sf
from fastapi import FastAPI
from qwen_asr_app.ai_secrets import AISecretStore
from qwen_asr_app import ai
from qwen_asr_app.storage import RecordingRecord
from qwen_asr_app.note_history import versions, read_version
from qwen_asr_app.note_routes import note_router
from qwen_asr_app.upload_jobs import UploadJobs
from qwen_asr_app.library_search import LibrarySearch
from qwen_asr_app.audio import decode_audio_file, convert_audio_to_aac, make_playback_audio


def fixture(root, ident='upgrade-record-0001'):
    folder = root / ident
    folder.mkdir()
    record = RecordingRecord(folder, {'id':ident, 'kind':'upload', 'status':'completed', 'title':'课堂',
        'category':'通信原理','sample_count':32000, 'started_at':'2026-09-23T10:00:00+08:00'})
    record.save_all()
    return record


class CredentialTests(unittest.TestCase):
    def test_bound_key_never_follows_url_change(self):
        with tempfile.TemporaryDirectory() as raw:
            store=AISecretStore(Path(raw)/'keys.json')
            store.update({'correction': {'api_key':'dummy', 'base_url':'https://a.invalid/v1/chat/completions'}})
            self.assertEqual(store.get('correction','https://a.invalid/v1'), 'dummy')
            self.assertEqual(store.get('correction','https://b.invalid/v1'), '')
            self.assertEqual(store.get('summary','https://a.invalid/v1'), '')

    def test_legacy_unbound_key_is_not_sent_and_env_has_endpoint_scope(self):
        with tempfile.TemporaryDirectory() as raw:
            store=AISecretStore(Path(raw)/'keys.json')
            store.update({'summary':'legacy'})
            self.assertEqual(store.get('summary','https://b.invalid/v1'),'')
            with patch.object(ai, 'ai_secret_store', store), patch.dict(os.environ, {'AI_API_KEY':'env-dummy','AI_BASE_URL':'https://a.invalid/v1'}):
                base,key=ai.provider_credentials({'ai':{'summary_provider':{'base_url':'https://b.invalid/v1'}}},'summary')
                self.assertEqual(key,'')
                self.assertEqual(ai.provider_credentials({'ai':{'summary_provider':{'base_url':'https://a.invalid/v1'}}},'summary')[1],'env-dummy')

    def test_environment_endpoint_cannot_borrow_top_level_key(self):
        with patch.dict(os.environ, {'AI_REVIEW_BASE_URL': 'https://review.invalid/v1'}):
            provider = ai.provider_for_mode({'base_url': 'https://original.invalid/v1', 'api_key': 'dummy'}, 'review')
            self.assertFalse(provider['api_key'])



class UpgradeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_account_concurrency_spans_models_and_features(self):
        current=peak=0
        async def worker(**kwargs):
            nonlocal current,peak
            current+=1;peak=max(peak,current)
            await asyncio.sleep(.01)
            current-=1
            return 'ok'
        provider={'base_url':'https://cap-test.invalid/v1','model':'demo','concurrency':8,'api_key':'dummy'}
        config={'ai':{'summary_provider':provider,'correction_provider':{**provider,'model':'other'}}}
        with patch.dict(os.environ, {'QWEN_ASR_AI_TOTAL_CONCURRENCY':'2'}), patch.object(ai,'_request_ai_text_unlimited',worker):
            await asyncio.gather(*(ai.request_ai_text(instructions='',payload={},config=config,mode=mode,max_output_tokens=128) for mode in ['summary']*4+['correction']*4))
        self.assertEqual(peak,2)

    async def test_cancel_user_intent_is_distinct_from_shutdown(self):
        import app
        with tempfile.TemporaryDirectory() as raw:
            record=fixture(Path(raw)); started=asyncio.Event()
            store=SimpleNamespace(find=lambda ident:record)
            async def runner():
                started.set(); await asyncio.Event().wait()
            with patch.object(app,'recording_store',store):
                app._start_aux_ai_job(record,'refinement',runner)
                task=app.background_ai_tasks['refinement:'+record.id]
                await started.wait()
                await app.ai_task_action({'recording_id':record.id,'type':'refinement','action':'cancel'})
                await asyncio.gather(task,return_exceptions=True)
                self.assertEqual(record.metadata['ai_jobs']['refinement']['status'],'cancelled')
                started.clear()
                app._start_aux_ai_job(record,'refinement',runner)
                task=app.background_ai_tasks['refinement:'+record.id]
                await started.wait();task.cancel();await asyncio.gather(task,return_exceptions=True)
                self.assertEqual(record.metadata['ai_jobs']['refinement']['status'],'interrupted')

    async def test_note_versions_restore_and_conflict_with_seekable_audio(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);record=fixture(root)
            wav=record.directory/'recording.wav'
            sf.write(wav,np.zeros(32000,dtype=np.float32),16000)
            record.update_note_summary('原始笔记：采样定理')
            record.update_note_summary('人工修订：带宽')
            old=versions(record)[0]['id']
            store=SimpleNamespace(find=lambda ident:RecordingRecord.load(record.directory),list_records=lambda:[record])
            service=FastAPI();service.include_router(note_router(store,lambda fn:fn))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service),base_url='http://test') as client:
                diff=await client.get(f'/api/recordings/{record.id}/versions/{old}')
                self.assertIn('-原始笔记',diff.json()['diff'])
                conflict=await client.post(f'/api/recordings/{record.id}/versions/{old}/restore',json={'expected_summary':'stale'})
                self.assertEqual(conflict.status_code,409)
                restored=await client.post(f'/api/recordings/{record.id}/versions/{old}/restore',json={'expected_summary':'人工修订：带宽'})
                self.assertEqual(restored.status_code,200)
                self.assertEqual(len(versions(record)),2)
                audio=await client.get(f'/api/recordings/{record.id}/playback',headers={'Range':'bytes=0-63'})
                self.assertEqual(audio.status_code,206)
                self.assertEqual(len(audio.content),64)

    async def test_http_rejects_untrusted_host_and_origin(self):
        import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://127.0.0.1:8765') as client:
            for headers in ({'Host':'untrusted.invalid'}, {'Origin':'https://untrusted.invalid'}, {'Sec-Fetch-Site':'cross-site'}):
                result=await client.post('/api/ai/secrets',headers=headers,json={})
                self.assertEqual(result.status_code,403)
            self.assertEqual((await client.get('/api/config')).status_code,200)

    async def test_upload_survives_subscriber_close_and_recovers_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);record=fixture(root);record.metadata['status']='interrupted';started=asyncio.Event();release=asyncio.Event()
            async def runner(source,name,config,**kwargs):
                yield json.dumps({'type':'accepted','session':record.public()}).encode()
                started.set();await release.wait()
                yield json.dumps({'type':'complete','session':record.public(),'progress':100}).encode()
            with patch('qwen_asr_app.upload_jobs.PRIVATE_ROOT',root):
                jobs=UploadJobs(runner,lambda ident:record,lambda config:{},lambda config:None)
                source=root/'input.wav';source.write_bytes(b'input')
                job=jobs.create(source,'input.wav',{})
                await started.wait()
                stream=jobs.stream(job['id']);await anext(stream);await stream.aclose()
                self.assertFalse(jobs.tasks[job['id']].done())
                reloaded=UploadJobs(runner,lambda ident:record,lambda config:{},lambda config:None)
                self.assertEqual(reloaded.jobs[job['id']]['status'],'interrupted')
                self.assertTrue(Path(job['source_path']).exists())
                release.set();await jobs.tasks[job['id']];await asyncio.sleep(0)
                self.assertEqual(job['status'],'completed')
                self.assertFalse(Path(job['source_path']).exists())

    async def test_queued_upload_cancel_does_not_discard_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            with patch('qwen_asr_app.upload_jobs.PRIVATE_ROOT',root):
                jobs=UploadJobs(None,lambda ident:None,lambda c:c,lambda c:None)
                await jobs.lock.acquire()
                source=root/'source.wav';source.write_bytes(b'input')
                job=jobs.create(source,'source.wav',{})
                await jobs.action(job['id'],'cancel')
                await asyncio.gather(jobs.tasks[job['id']],return_exceptions=True);await asyncio.sleep(0)
                self.assertEqual(job['status'],'cancelled')
                self.assertTrue(Path(job['source_path']).exists())
                jobs.lock.release()

    async def test_upload_recovers_archive_saved_before_job_acknowledgement(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = fixture(root, 'upload-before-ack-0001')
            record.metadata['status'] = 'interrupted'
            seen = []
            async def runner(source, name, config, **kwargs):
                seen.append(kwargs['existing_record'])
                yield json.dumps({'type': 'complete', 'progress': 100, 'session': record.public()}).encode()
            with patch('qwen_asr_app.upload_jobs.PRIVATE_ROOT', root):
                jobs = UploadJobs(runner, lambda ident: record if ident == record.id else None, lambda c: c, lambda c: None)
                source = root / 'source.wav'; source.write_bytes(b'input')
                job = {'id': 'before-ack', 'filename': 'source.wav', 'status': 'interrupted',
                       'recording_id': '', 'source_path': str(source), 'progress': 0,
                       'config': {'_upload_recording_id': record.id}, 'stage': '', 'error': ''}
                jobs.jobs[job['id']] = job
                await jobs.action(job['id'], 'continue')
                await jobs.tasks[job['id']]
                self.assertEqual(seen, [record])
                self.assertEqual(job['recording_id'], record.id)

    async def test_library_resume_reuses_task_and_respects_busy_record(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); record = fixture(root)
            record.metadata['status'] = 'interrupted'
            async def runner(*args, **kwargs):
                await asyncio.Event().wait()
                yield b'{}'
            with patch('qwen_asr_app.upload_jobs.PRIVATE_ROOT', root):
                guard = unittest.mock.Mock()
                jobs = UploadJobs(runner, lambda ident: record, lambda c: c, lambda c: None, guard)
                first = jobs.create(None, 'audio.wav', {}, record)
                await jobs.action(first['id'], 'pause')
                await asyncio.gather(jobs.tasks[first['id']], return_exceptions=True)
                await asyncio.sleep(0)
                second = jobs.create(None, 'audio.wav', {}, record)
                self.assertIs(first, second)
                self.assertEqual(len(jobs.jobs), 1)
                await jobs.action(first['id'], 'cancel')
                await asyncio.gather(jobs.tasks[first['id']], return_exceptions=True)
                await asyncio.sleep(0)
                guard.side_effect = ValueError('录音正在生成笔记')
                with self.assertRaisesRegex(ValueError, '生成笔记'):
                    await jobs.action(first['id'], 'continue')

    async def test_invalid_upload_does_not_load_model(self):
        import app
        from qwen_asr_app.upload_routes import upload_router
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch('qwen_asr_app.upload_jobs.PRIVATE_ROOT', root), patch.object(app.model_manager, 'load', new=AsyncMock()) as load:
                jobs = UploadJobs(app.batch_transcription_stream, lambda ident: None, app.redacted_batch_config, lambda c: None)
                service = FastAPI()
                service.include_router(upload_router(jobs, None, lambda ident: None))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://test') as client:
                    response = await client.post('/api/upload-jobs?background=true', files={'file': ('broken.wav', b'not audio')})
                    self.assertEqual(response.status_code, 202)
                    ident = response.json()['id']
                    await jobs.tasks[ident]
                    status = await client.get('/api/upload-jobs/' + ident)
                    self.assertEqual(status.json()['status'], 'failed')
                    self.assertTrue(Path(jobs.jobs[ident]['source_path']).exists())
                load.assert_not_awaited()

    async def test_real_upload_pipeline_with_mock_asr(self):
        import app
        from qwen_asr_app.upload_routes import upload_router
        import io
        wav = io.BytesIO()
        sf.write(wav, np.zeros(16000, dtype=np.float32), 16000, format='WAV')
        with tempfile.TemporaryDirectory() as raw:
            with patch('qwen_asr_app.upload_jobs.PRIVATE_ROOT', Path(raw)), \
                 patch.object(app.model_manager, 'load', new=AsyncMock()), \
                 patch.object(app.model_manager, 'transcribe', new=AsyncMock(return_value=('课堂测试内容', 'Chinese', .01))):
                jobs = UploadJobs(app.batch_transcription_stream, app.recording_store.find, app.redacted_batch_config, lambda c: None)
                service = FastAPI()
                service.include_router(upload_router(jobs, app.recording_store, lambda ident: None))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://test') as client:
                    response = await client.post('/api/upload-jobs?background=true', files={'file': ('class.wav', wav.getvalue())})
                    ident = response.json()['id']
                    await jobs.tasks[ident]; await asyncio.sleep(0)
                    self.assertEqual(jobs.jobs[ident]['status'], 'completed', jobs.jobs[ident].get('error'))
                    events = await client.get('/api/upload-jobs/' + ident + '/events')
                    result = json.loads(events.text.strip())
                    self.assertEqual(result['session']['segments'][0]['raw'], '课堂测试内容')
                    self.assertEqual(result['session']['id'], 'upload-' + ident)


class SearchAudioTests(unittest.TestCase):
    def test_search_tracks_note_changes_and_deleted_recordings(self):
        with tempfile.TemporaryDirectory() as raw:
            record=fixture(Path(raw));index=LibrarySearch()
            record.upsert_segment({'id':1,'start':1,'end':2,'raw':'奈奎斯特采样定理'})
            result=index.search([record],'采样')
            self.assertEqual(result['matches'][0]['start'],1)
            record.update_note_summary('傅里叶变换')
            self.assertEqual(index.search([record],'傅里叶')['total'],1)
            self.assertEqual(index.search([],'傅里叶')['total'],0)
            self.assertFalse(index.cache)

    def test_decode_is_disk_backed_and_aac_is_seekable(self):
        import av
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);wav=root/'a.wav'
            sf.write(wav,np.sin(np.arange(16000)*.03).astype(np.float32)*.1,16000)
            decoded=decode_audio_file(wav)
            self.assertIsInstance(decoded,np.memmap)
            self.assertEqual(len(decoded),16000)
            aac=root/'a.aac';convert_audio_to_aac(wav,aac)
            m4a=root/'a.m4a';make_playback_audio(aac,m4a)
            with av.open(str(m4a)) as container:
                self.assertGreater(container.duration,0)
                container.seek(500000)
                self.assertTrue(next(container.decode(audio=0)).samples)

    def test_new_library_layout_is_in_data_snapshot(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('audit_snapshot',Path(__file__).resolve().parents[1]/'scripts/check_tests.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);library=root/'library';recording=library/'课程'/'上课录音'/'时间'
            recording.mkdir(parents=True);meta=recording/'session.json';meta.write_text('{}')
            (root/'app-settings.json').write_text(json.dumps({'storage_root':str(library)}))
            module.ROOT=root
            with patch.object(Path,'home',return_value=root/'home'):
                self.assertIn(str(meta),module.snapshot())
