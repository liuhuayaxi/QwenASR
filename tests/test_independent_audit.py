"""Independent-audit acceptance at provider, task and HTTP boundaries.

Only temporary data, ASGI requests and mocked compute/transport are used.
"""
from tests import isolation
import asyncio
import copy
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import numpy as np
from fastapi import FastAPI
from qwen_asr_app import inference_providers as providers
from qwen_asr_app import remote_operations as operations
from qwen_asr_app.pdf_jobs import PDFJobs
from qwen_asr_app.upload_jobs import UploadJobs
from qwen_asr_app.model_runtime import ModelManager
from qwen_asr_app.task_center import task_center_router
from qwen_asr_app.mineru_settings import normalize_mineru_settings


class LocalIndependence(unittest.IsolatedAsyncioTestCase):
    def test_local_document_ignores_bad_remote_url_token_and_missing_node(self):
        local = Mock(return_value={'ok': True})
        with patch.object(providers, 'remote_client', side_effect=AssertionError('remote imported')):
            result = providers.parse_document('/missing', '/unused', local,
                config={'mineru_provider': 'local', 'asr_provider': 'remote_only',
                        'base_url': 'bad', 'token_file': '/missing'})
        self.assertEqual(local.call_count, 1)
        self.assertEqual(result['provider'], 'local')

    async def test_local_asr_ignores_remote_settings_and_calls_original_runner(self):
        manager = ModelManager.__new__(ModelManager)
        manager._transcribe_local = AsyncMock(return_value=('local', 'Chinese', .1))
        with patch.object(providers.inference_settings, 'read', side_effect=AssertionError('settings read')), \
             patch.object(providers, 'remote_client', side_effect=AssertionError('remote imported')):
            result = await manager.transcribe(np.zeros(10), None, '',
                inference_config={'asr_provider': 'local', 'base_url': 'bad'})
        self.assertEqual(result[0], 'local')
        manager._transcribe_local.assert_awaited_once()

    async def test_legacy_batch_without_snapshot_is_pinned_local(self):
        import app
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'legacy.wav'
            source.write_bytes(b'synthetic')
            record = SimpleNamespace(
                metadata={}, id='legacy-local-test', segments=[], audio_path=source,
                save_all=Mock(), save_metadata=Mock(), public=Mock(),
                upsert_segment=lambda value: record.segments.append(value),
                complete=lambda: record.metadata.update(status='completed'),
            )
            record.public.return_value = {'id': record.id, 'segments': record.segments}
            with patch.object(app.model_manager, 'load', new=AsyncMock()), \
                 patch.object(app, 'decode_audio_file', return_value=np.zeros(16000, dtype=np.float32)), \
                 patch.object(providers.inference_settings, 'for_task', side_effect=AssertionError('global settings read')), \
                 patch.object(app, 'vad_split_ranges', return_value=[(0, 16000)]), \
                 patch.object(app.model_manager, 'transcribe', new=AsyncMock(return_value=('legacy', 'Chinese', .01))):
                events = [json.loads(value) async for value in app.batch_transcription_stream(
                    None, 'legacy.wav', {}, existing_record=record
                )]
        self.assertEqual(events[-1]['type'], 'complete')
        self.assertEqual(record.metadata['inference_settings']['asr_provider'], 'local')

    async def test_force_local_load_does_not_read_provider_configuration(self):
        manager = ModelManager.__new__(ModelManager)
        manager.cancel_unload = Mock()
        manager.load_lock = asyncio.Lock()
        manager.worker = Mock(is_alive=Mock(return_value=True))
        with patch.object(providers.inference_settings, 'read', side_effect=AssertionError('settings read')):
            await manager.load(force_local=True)

    def test_legacy_local_missing_snapshot_does_not_use_current_remote_mode(self):
        with patch.object(providers.inference_settings, 'read', side_effect=AssertionError('settings read')):
            self.assertEqual(operations.saved_config()['asr_provider'], 'local')
        with self.assertRaises(ValueError): operations.saved_config(remote_reference=True)

    def test_unknown_error_is_not_automatic_fallback(self):
        local = Mock()
        with patch.object(providers.RemoteMinerUProvider, 'parse', side_effect=ValueError('bad JSON')):
            with self.assertRaises(ValueError):
                providers.parse_document('/missing', '/unused', local,
                    config={**providers.DEFAULTS, 'mineru_provider': 'remote_preferred'})
        local.assert_not_called()

    def test_only_proven_nonacceptance_enables_fallback_and_preserves_receipt(self):
        transport = providers.remote_client(providers.DEFAULTS)
        module = __import__(transport.__class__.__module__)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)/'private/receipt.json'
            operations.atomic_json(path, {'state': 'proven_not_accepted', 'key': 'fixed'})
            local = Mock(return_value={'ok': True})
            with patch.object(providers.RemoteMinerUProvider, 'parse', side_effect=module.ProvenNotAccepted('offline')):
                result = providers.parse_document('/missing', Path(raw)/'out', local,
                    config={**providers.DEFAULTS, 'mineru_provider': 'remote_preferred'}, receipt_path=path)
                self.assertTrue(result['fallback'])
                self.assertEqual(operations.receipt(path)['state'], 'local_fallback')
                with self.assertRaises(module.ProvenNotAccepted):
                    providers.parse_document('/missing', Path(raw)/'out', local,
                        config={**providers.DEFAULTS, 'mineru_provider': 'remote_only'}, receipt_path=path)
            self.assertEqual(local.call_count, 1)

    def test_audio_snapshot_preserves_bytes_and_parameters_and_omits_secrets(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            first = providers.prepare_audio(folder, np.zeros(16000), 'Chinese', 'saved prompt', 512,
                {**providers.DEFAULTS, 'asr_provider': 'remote_only', 'token': 'never-persist'}, {'max_tokens': 4096})
            before = (folder/'audio.wav').read_bytes()
            restored = providers.prepare_audio(folder, np.ones(12), 'English', 'new prompt', 128,
                {'base_url': 'bad'}, {'temperature': 1.0})
            self.assertEqual(first, restored)
            self.assertEqual(restored['max_new_tokens'], 4096)
            self.assertEqual(before, (folder/'audio.wav').read_bytes())
            self.assertNotIn('never-persist', (folder/'request.json').read_text())

    def test_office_remote_only_has_no_local_dependency_preflight(self):
        from qwen_asr_app import course_file_conversion as conversion
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root/'native.docx'; source.write_bytes(b'synthetic-native')
            def parse(source, output, *args):
                output.mkdir(); (output/'markdown.md').write_text('Synthetic official Flash result')
                with zipfile.ZipFile(output/'result.zip','w') as z: z.writestr('markdown.md','Synthetic')
                return {'provider':'remote','metrics':{'tier':'flash'}}
            with patch.object(conversion, 'find_mineru_binary', side_effect=AssertionError('local mineru')), \
                 patch.object(conversion, 'office_binary', side_effect=AssertionError('local office')), \
                 patch.object(conversion.mineru_settings, 'read', side_effect=AssertionError('local settings')), \
                 patch.object(providers.RemoteMinerUProvider, 'parse', side_effect=parse):
                result = conversion.convert(source, root/'result.pdf',
                    inference_config={**providers.DEFAULTS,'mineru_provider':'remote_only'})
            self.assertEqual(result['inference']['metrics']['tier'],'flash')
            self.assertFalse(result['visual_original_preserved'])


class TaskBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_pdf_wait_does_not_block_new_local_compute(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); pdf = root/'fixture.pdf'; pdf.write_bytes(b'%PDF-synthetic')
            remote_started, release_remote, local_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
            jobs = PDFJobs(asyncio.Lock()); jobs.jobs.clear()
            async def worker(fn, source, **kwargs):
                mode=kwargs['inference_config']['mineru_provider']
                if mode != 'local':
                    self.assertFalse(jobs.lock.locked())
                    remote_started.set(); await release_remote.wait()
                else:
                    self.assertTrue(jobs.lock.locked()); local_done.set()
                return root/(mode+'.pdf')
            with patch.object(jobs,'save'), patch.object(operations,'PRIVATE_ROOT',root/'private'), \
                 patch('qwen_asr_app.pdf_jobs.find_mineru_binary',return_value=Path('/synthetic')), \
                 patch('qwen_asr_app.pdf_jobs.conversion_worker', side_effect=worker):
                remote=jobs.start([str(pdf)],{**providers.DEFAULTS,'mineru_provider':'remote_only'})
                await asyncio.wait_for(remote_started.wait(),1)
                another=root/'local.pdf'; another.write_bytes(b'%PDF-local')
                local=jobs.start([str(another)],{'mineru_provider':'local'})
                await asyncio.wait_for(local_done.wait(),1)
                release_remote.set()
                await asyncio.gather(*list(jobs.tasks.values()))
                self.assertEqual(jobs.jobs[local['id']]['status'],'completed')
                self.assertEqual(jobs.jobs[remote['id']]['status'],'completed')

    async def test_upload_remote_wait_releases_local_upload_queue(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); started=asyncio.Event(); release=asyncio.Event(); local_done=asyncio.Event()
            async def runner(source, filename, config, **kwargs):
                if config['inference_settings']['asr_provider'] != 'local':
                    started.set(); await release.wait()
                else: local_done.set()
                yield json.dumps({'type':'complete','progress':100})
            jobs=UploadJobs(runner,lambda ident:None,copy.deepcopy,lambda config:None)
            jobs.root=root; jobs.path=root/'jobs.json'; jobs.jobs.clear()
            for name in ('remote.wav','local.wav'): (root/name).write_bytes(b'audio')
            remote=jobs.create(root/'remote.wav','remote.wav',{'inference_settings':{**providers.DEFAULTS,'asr_provider':'remote_only'}})
            await asyncio.wait_for(started.wait(),1)
            jobs.create(root/'local.wav','local.wav',{'inference_settings':{'asr_provider':'local'}})
            await asyncio.wait_for(local_done.wait(),1)
            release.set(); await asyncio.gather(*list(jobs.tasks.values()))

    async def test_upload_create_preserves_existing_attempt_and_all_parameters(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); source=root/'a.wav'; source.write_bytes(b'audio')
            jobs=UploadJobs(None,lambda ident:None,copy.deepcopy,lambda config:None)
            jobs.root=root; jobs.path=root/'jobs.json'; jobs.jobs.clear()
            with patch.object(jobs,'schedule'):
                job=jobs.create(source,'a.wav',{'language':'Chinese','asr_params':{'max_tokens':4096},
                    'inference_settings':{**providers.DEFAULTS,'asr_provider':'remote_only'}})
                before=copy.deepcopy(job['config'])
                record=SimpleNamespace(id=job['recording_id'],metadata={'batch_options':before})
                again=jobs.create(None,'a.wav',{'language':'English'},record)
                self.assertEqual(again['config'],before)
                await jobs.action(job['id'],'cancel')
                self.assertTrue((Path(before['_remote_operation_dir'])/'receipt.cancel.json').exists())

    async def test_remote_partial_never_calls_local_or_remote_model(self):
        from qwen_asr_app.live_session import TranscriptionSession
        session=TranscriptionSession.__new__(TranscriptionSession)
        session.config={'inference_settings':{'asr_provider':'remote_only'}}
        session.model_manager=Mock();session.partial_running=True
        await session.run_partial(np.zeros(160),1)
        session.model_manager.transcribe.assert_not_called()


class HttpAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_and_local_capabilities_survive_bad_remote_url(self):
        import app
        with tempfile.TemporaryDirectory() as raw:
            cfg=providers.InferenceSettings(Path(raw)/'settings.json')
            cfg.path.write_text(json.dumps({'asr_provider':'local','mineru_provider':'local','base_url':'bad'}))
            with patch.object(providers,'inference_settings',cfg), patch.object(providers,'remote_client',side_effect=AssertionError('network')):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://test') as client:
                    self.assertEqual((await client.get('/api/health')).status_code,200)
                    from qwen_asr_app.course_file_conversion import capabilities
                    self.assertIn('mineru',capabilities())

    async def test_settings_api_keeps_local_mode_independent_of_remote_url(self):
        import app
        with patch.object(providers, 'remote_client', side_effect=AssertionError('network')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                local = await client.put('/api/inference/settings', json={'settings': {
                    **providers.DEFAULTS, 'asr_provider': 'local', 'mineru_provider': 'local',
                    'base_url': 'not-a-url'}})
                self.assertEqual(local.status_code, 200, local.text)
                remote = await client.put('/api/inference/settings', json={'settings': {
                    **providers.DEFAULTS, 'asr_provider': 'remote_only', 'mineru_provider': 'local',
                    'base_url': 'not-a-url'}})
                self.assertEqual(remote.status_code, 400)

    async def test_task_api_new_local_requires_duplicate_ack_and_preserves_old_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); source=root/'source.pdf'; source.write_bytes(b'%PDF-synthetic')
            jobs=PDFJobs(asyncio.Lock()); jobs.jobs.clear()
            with patch.object(jobs,'save'),patch.object(jobs,'schedule'),patch.object(operations,'PRIVATE_ROOT',root/'private'):
                old=jobs.start([str(source)],{**providers.DEFAULTS,'mineru_provider':'remote_only'})
            job=jobs.jobs[old['id']];job['status']='failed'
            receipt_path=Path(job['files'][0]['operation']['receipt_path'])
            operations.atomic_json(receipt_path,{'state':'submitting','key':'old-fixed-key'})
            courses=SimpleNamespace(store=SimpleNamespace(settings=SimpleNamespace(categories=[])),tasks={})
            api=FastAPI();api.include_router(task_center_router(lambda **kwargs:[],None,courses,jobs))
            headers={'X-QwenASR-Client':'task-center'}
            with patch('qwen_asr_app.task_center.discover_courses'),patch('qwen_asr_app.task_center.course_tasks',return_value=[]), \
                 patch('qwen_asr_app.pdf_jobs.find_mineru_binary',return_value=Path('/synthetic')),patch.object(jobs,'save'),patch.object(jobs,'schedule'):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as client:
                    feed=(await client.get('/api/tasks')).json()['tasks']
                    self.assertIn('new_local',feed[0]['actions'])
                    body={'id':'pdf:'+old['id'],'action':'new_local'}
                    denied=await client.post('/api/tasks/action',headers=headers,json=body)
                    self.assertEqual(denied.status_code,409)
                    accepted=await client.post('/api/tasks/action',headers=headers,json={**body,'acknowledge_duplicate':True})
                    self.assertEqual(accepted.status_code,200,accepted.text)
                    self.assertNotEqual(accepted.json()['id'],old['id'])
                    self.assertEqual(accepted.json()['inference_settings']['mineru_provider'],'local')
            self.assertEqual(operations.receipt(receipt_path)['key'],'old-fixed-key')

if __name__=='__main__':unittest.main()
