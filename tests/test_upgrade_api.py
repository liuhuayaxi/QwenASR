"""API acceptance for the combined Mac release; all data is disposable."""
from tests import isolation  # noqa: F401
import asyncio
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from qwen_asr_app import inference_providers as providers
import qwen_asr_app.task_center as taskcenter
from qwen_asr_app import remote_operations as operations
from qwen_asr_app.pdf_jobs import PDFJobs
from qwen_asr_app.task_center import task_center_router
from qwen_asr_app.upload_jobs import UploadJobs


class UpgradeAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qwenasr-upgrade-api-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, name in ((operations, 'PRIVATE_ROOT'),
                             (__import__('qwen_asr_app.pdf_jobs', fromlist=['']), 'PRIVATE_ROOT'),
                             (__import__('qwen_asr_app.upload_jobs', fromlist=['']), 'PRIVATE_ROOT')):
            manager = patch.object(target, name, self.root/'private')
            manager.start()
            self.addCleanup(manager.stop)
        self.pdfs = PDFJobs(asyncio.Lock())
        manager = patch.object(self.pdfs, 'schedule')
        manager.start()
        self.addCleanup(manager.stop)
        self.settings = providers.InferenceSettings(self.root/'providers.json')
        manager = patch.object(providers, 'inference_settings', self.settings)
        manager.start()
        self.addCleanup(manager.stop)
        self.courses = SimpleNamespace(
            store=SimpleNamespace(settings=SimpleNamespace(categories=[], storage_root=self.root/'library')), tasks={})
        self.headers = {'X-QwenASR-Client': 'task-center'}
        manager = patch.object(taskcenter, 'PRIVATE_ROOT', self.root/'private')
        manager.start()
        self.addCleanup(manager.stop)

    def api(self, uploads=None):
        api = FastAPI()
        api.include_router(task_center_router(lambda **kwargs: [], None, self.courses, self.pdfs, uploads))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test')

    async def test_completed_tasks_have_no_actions_and_cleanup_keeps_result(self):
        output = self.root/'saved.pdf'
        output.write_bytes(b'%PDF-saved-result')
        self.pdfs.jobs['done'] = {'id': 'done', 'status': 'completed', 'created_at': 1,
            'updated_at': 2, 'files': [{'source_name': 'synthetic.pdf', 'source_path': str(self.root/'missing.pdf'),
                       'inference': {'provider': 'remote', 'metrics': {'gpu_release_verified': True}},
                       'status': 'completed', 'output_path': str(output), 'progress': 100}]}
        with patch('qwen_asr_app.task_center.discover_courses'), patch('qwen_asr_app.task_center.course_tasks', return_value=[]):
            async with self.api() as client:
                feed = (await client.get('/api/tasks')).json()
                task = next(item for item in feed['tasks'] if item['id'] == 'pdf:done')
                self.assertEqual(task['actions'], [])
                self.assertTrue(task['inference']['metrics']['gpu_release_verified'])
                cleared = await client.post('/api/tasks/clear-completed', headers=self.headers,
                    json={'items': [{'id': task['id'], 'generation': task['history_generation']}]})
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertEqual(cleared.json()['cleared'], 1)
                self.assertFalse((await client.get('/api/tasks')).json()['tasks'])
                self.assertTrue(output.is_file())
                # A fresh router (as after restart) reads the same archive.
                async with self.api() as restarted:
                    self.assertFalse((await restarted.get('/api/tasks')).json()['tasks'])
                self.pdfs.jobs['done']['status'] = 'running'
                self.assertEqual((await client.get('/api/tasks')).json()['tasks'][0]['status'], 'running')
                again = await client.post('/api/tasks/clear-completed', headers=self.headers, json={})
                self.assertEqual(again.json()['cleared'], 0)
                self.pdfs.jobs['done'].update(status='completed', updated_at=3)
                self.assertEqual((await client.get('/api/tasks')).json()['tasks'][0]['status'], 'completed')
                self.pdfs.jobs['done']['updated_at'] = 2
                restored = await client.post('/api/tasks/restore-completed', headers=self.headers, json={})
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertEqual(restored.json()['restored'], 1)
                self.assertEqual((await client.get('/api/tasks')).json()['tasks'][0]['id'], 'pdf:done')

    async def test_remote_queue_is_displayed_as_queued_before_completed(self):
        path = self.root/'private/remote-receipt.json'
        operations.atomic_json(path, {'state': 'queued', 'job_id': 'queued-job', 'key': 'queued-key'})
        item = {'id': 'a' * 32, 'name': 'queued.pdf', 'conversion': {
            'status': 'running', 'updated_at': '2026-10-10T10:00:00+08:00',
            'inference_settings': {'mineru_provider': 'remote_only'},
            'operation': {'receipt_path': str(path)}, 'inference': {}}}
        self.courses.store.settings.categories = ['synthetic']
        running = {'id': 'b' * 32, 'name': 'running.pdf', 'conversion': {
            'status': 'running', 'updated_at': '2026-10-10T09:00:00+08:00',
            'progress': 42.5, 'progress_estimate': {'estimated': True, 'percent': 42.5, 'remaining_seconds': 180},
            'remote_operation': {'state': 'running'},
            'inference_settings': {'mineru_provider': 'remote_only'}}}
        done = {'id': 'c' * 32, 'name': 'done.pdf', 'conversion': {
            'status': 'completed', 'updated_at': '2026-10-10T11:00:00+08:00',
            'inference_settings': {'mineru_provider': 'remote_only'}}}
        self.courses.store.list = lambda category: [done, item, running]
        self.courses.tasks[('synthetic', item['id'])] = object()
        self.courses.tasks[('synthetic', running['id'])] = object()
        with patch('qwen_asr_app.task_center.discover_courses'), patch('qwen_asr_app.task_center.course_tasks', return_value=[]):
            async with self.api() as client:
                feed = (await client.get('/api/tasks')).json()
        self.assertEqual([t['title'] for t in feed['tasks']], ['running.pdf', 'queued.pdf', 'done.pdf'])
        self.assertEqual(feed['tasks'][0]['progress_estimate']['remaining_seconds'], 180)
        self.assertEqual(feed['tasks'][0]['progress'], 42.5)
        self.assertEqual(feed['tasks'][1]['status'], 'queued')
        self.assertEqual(feed['tasks'][1]['remote_operation_state'], 'queued')
        self.assertEqual(feed['tasks'][2]['actions'], [])

    async def test_course_receipt_provenance_survives_restart_and_is_public_only(self):
        path = self.root/'private/receipt.json'
        operations.atomic_json(path, {'state': 'cancel_unconfirmed', 'job_id': 'saved-job',
            'key': 'saved-key', 'token': 'synthetic-private', 'blob_path': '/synthetic-private'})
        state = {'status': 'pending', 'inference_settings': {'mineru_provider': 'remote_only'},
                 'operation': {'receipt_path': str(path)},
                 'inference': {'provider': 'remote', 'fallback_reason': ''}}
        item = {'id': 'asset', 'name': 'synthetic.pdf', 'conversion': state}
        self.courses.store.settings.categories = ['synthetic']
        self.courses.store.list = lambda category: [item]
        before = path.read_bytes()
        with patch('qwen_asr_app.task_center.discover_courses'), \
             patch('qwen_asr_app.task_center.course_tasks', return_value=[]):
            async with self.api() as client:
                response = await client.get('/api/tasks')
                task = response.json()['tasks'][0]
        self.assertEqual(response.status_code, 200)
        for key, value in {'configured_provider': 'remote_only', 'actual_provider': 'remote',
            'remote_job_id': 'saved-job', 'remote_operation_state': 'cancel_unconfirmed',
            'remote_operation_key': 'saved-key', 'fallback_reason': ''}.items():
            self.assertEqual(task[key], value)
        self.assertNotIn('synthetic-private', response.text)
        self.assertEqual(before, path.read_bytes())

    async def test_local_create_and_legacy_continue_ignore_remote_settings(self):
        source = self.root/'synthetic.pdf'
        source.write_bytes(b'%PDF-synthetic')
        with patch('qwen_asr_app.pdf_jobs.find_mineru_binary', return_value=Path('/synthetic')), \
             patch('qwen_asr_app.task_center.find_mineru_binary', return_value=Path('/synthetic')), \
             patch('qwen_asr_app.task_center.discover_courses'), \
             patch('qwen_asr_app.task_center.course_tasks', return_value=[]), \
             patch.object(providers, 'remote_client', side_effect=AssertionError('remote access')):
            async with self.api() as client:
                response = await client.post('/api/tasks/pdf', headers=self.headers, json={'paths': [str(source)]})
                self.assertEqual(response.status_code, 200)
                first = response.json()['job']
                self.assertEqual(first['inference_settings']['mineru_provider'], 'local')
                legacy = self.pdfs.jobs[first['id']]
                legacy['status'] = 'interrupted'
                legacy.pop('inference_settings')
                self.settings.save({**providers.DEFAULTS, 'mineru_provider': 'remote_only'})
                for action in ('continue', 'retry'):
                    legacy['status'] = 'interrupted'
                    response = await client.post('/api/tasks/action', headers=self.headers,
                        json={'id': 'pdf:'+legacy['id'], 'action': action})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['id'], first['id'])
                    self.assertEqual(response.json()['inference_settings']['mineru_provider'], 'local')

    async def test_pdf_new_local_preserves_old_receipt_bundle_and_result(self):
        source = self.root/'synthetic.pdf'
        source.write_bytes(b'%PDF-synthetic')
        old = self.pdfs.start([str(source)], {**providers.DEFAULTS, 'mineru_provider': 'remote_only'})
        job = self.pdfs.jobs[old['id']]
        job['status'] = 'pending'
        path = Path(job['files'][0]['operation']['receipt_path'])
        operations.atomic_json(path, {'state': 'submitting', 'key': 'old-key', 'job_id': 'old-job'})
        bundle = path.parent/'downloaded-result.zip'
        result = path.parent/'result.txt'
        bundle.write_bytes(b'saved-bundle')
        result.write_bytes(b'saved-result')
        protected = {p: p.read_bytes() for p in (path, bundle, result)}
        with patch('qwen_asr_app.pdf_jobs.find_mineru_binary', return_value=Path('/synthetic')), \
             patch('qwen_asr_app.task_center.discover_courses'), \
             patch('qwen_asr_app.task_center.course_tasks', return_value=[]):
            async with self.api() as client:
                response = await client.post('/api/tasks/action', headers=self.headers,
                    json={'id': 'pdf:'+old['id'], 'action': 'new_local', 'acknowledge_duplicate': True})
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response.json()['id'], old['id'])
                self.assertEqual(response.json()['inference_settings']['mineru_provider'], 'local')
                feed = (await client.get('/api/tasks')).json()['tasks']
                remote = next(x for x in feed if x['id'] == 'pdf:'+old['id'])
                self.assertEqual(remote['remote_job_id'], 'old-job')
                self.assertEqual(remote['remote_operation_state'], 'submitting')
                self.assertEqual(remote['remote_operation_key'], 'old-key')
        self.assertTrue(all(p.read_bytes() == value for p, value in protected.items()))

    async def test_upload_continue_retry_keep_key_and_new_local_uses_new_key(self):
        uploads = UploadJobs(None, lambda ident: None, operations.without_secrets, lambda config: None)
        source = self.root/'synthetic.wav'
        source.write_bytes(b'synthetic audio')
        with patch.object(uploads, 'schedule'):
            old = uploads.create(source, 'synthetic.wav', {
                'inference_settings': {**providers.DEFAULTS, 'asr_provider': 'remote_only'}})
            old['status'] = 'pending'
            path = Path(old['config']['_remote_operation_dir'])/'receipt.json'
            operations.atomic_json(path, {'state': 'submitting', 'key': old['remote_operation_key'], 'job_id': 'old-job'})
            protected = path.read_bytes()
            before_config = copy.deepcopy(old['config'])
            with patch('qwen_asr_app.task_center.discover_courses'), \
                 patch('qwen_asr_app.task_center.course_tasks', return_value=[]):
                async with self.api(uploads) as client:
                    for action in ('continue', 'retry'):
                        old['status'] = 'pending'
                        response = await client.post('/api/tasks/action', headers=self.headers,
                            json={'id': 'upload:'+old['id'], 'action': action})
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()['remote_operation_key'], old['remote_operation_key'])
                        self.assertEqual(old['config'], before_config)
                    old['status'] = 'pending'
                    response = await client.post('/api/tasks/action', headers=self.headers,
                        json={'id': 'upload:'+old['id'], 'action': 'new_local', 'acknowledge_duplicate': True})
                    self.assertEqual(response.status_code, 200)
                    self.assertNotEqual(response.json()['remote_operation_key'], old['remote_operation_key'])
                    new = uploads.jobs[response.json()['id']]
                    self.assertEqual(new['config']['inference_settings']['asr_provider'], 'local')
            self.assertEqual(path.read_bytes(), protected)

    async def test_download_api_returns_saved_result_without_inference(self):
        output = self.root/'saved.pdf'
        output.write_bytes(b'%PDF-saved-result')
        self.pdfs.jobs['done'] = {'id': 'done', 'status': 'completed', 'created_at': 1,
            'files': [{'source_name': 'synthetic.pdf', 'source_path': str(self.root/'missing.pdf'),
                       'status': 'completed', 'output_path': str(output), 'progress': 100}]}
        with patch.object(providers, 'remote_client', side_effect=AssertionError('remote inference')), \
             patch('qwen_asr_app.pdf_jobs.convert_pdf_to_ai', side_effect=AssertionError('local inference')):
            async with self.api() as client:
                response = await client.get('/api/tasks/pdf/done/files/0')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.content).digest(), hashlib.sha256(output.read_bytes()).digest())
