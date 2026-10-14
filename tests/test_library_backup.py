from tests import isolation  # noqa: F401

import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

import app
from qwen_asr_app.library_backup import BackupManager, BackupSource, backup_router, backup_sources, build_backup


class BackupContentsTests(unittest.TestCase):
    def test_all_data_secrets_empty_folders_and_hashes_without_runtime_weights(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / '资料库'
            note = library / '物理光学/AI生成笔记/复习.pdf'
            note.parent.mkdir(parents=True)
            note.write_bytes(b'%PDF-test')
            (library / '物理光学/上课录音/空课次').mkdir(parents=True)
            private = root / 'private'
            private.mkdir()
            secret = private / 'ai-provider-secrets.json'
            secret.write_text('{"api_key":"synthetic-key"}')
            (private / 'runtimes').mkdir()
            (private / 'runtimes/large-model').write_bytes(b'do not export')
            settings_path = root / 'app-settings.json'
            settings_path.write_text('{"storage_root":"synthetic"}')
            (root / '.env').write_text('AI_API_KEY=synthetic-env-key')
            outside = root / 'external.m4a'
            outside.write_bytes(b'recording')
            legacy = root / 'legacy'
            legacy.mkdir()
            (legacy / 'obsolete.wav').write_bytes(b'old task')
            settings = SimpleNamespace(storage_root=library, known_roots=[str(root)], categories=['物理光学'])
            recordings = SimpleNamespace(list_records=lambda: [SimpleNamespace(directory=outside)])
            notes = SimpleNamespace(notes=lambda: [{'path': str(note)}])
            with patch('qwen_asr_app.library_backup.PRIVATE_ROOT', private), \
                 patch('qwen_asr_app.library_backup.ROOT', root), \
                 patch('qwen_asr_app.library_backup.SETTINGS_PATH', settings_path), \
                 patch('qwen_asr_app.library_backup.LEGACY_RECORDINGS_DIR', legacy), \
                 patch('qwen_asr_app.inference_providers.inference_settings.read', return_value={}):
                sources = backup_sources(settings, recordings, notes)
            destination = root / 'backup.zip'
            result = build_backup(sources, destination, lambda **kw: None)
            with zipfile.ZipFile(destination) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertIn('资料库/物理光学/上课录音/空课次/', names)
                self.assertEqual(archive.read('配置/private/ai-provider-secrets.json'), secret.read_bytes())
                self.assertIn(b'synthetic-env-key', archive.read('配置/.env'))
                self.assertFalse(any('large-model' in name for name in names))
                self.assertFalse(any('obsolete.wav' in name for name in names))
                self.assertEqual(sum(name.endswith('复习.pdf') for name in names), 1)
                manifest = json.loads(archive.read('备份清单.json'))
                self.assertTrue(manifest['contains_secrets'])
                self.assertEqual(result['file_count'], len(manifest['files']))
                for item in manifest['files']:
                    self.assertEqual(item['sha256'], hashlib.sha256(archive.read(item['path'])).hexdigest())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_changed_source_never_produces_a_successful_zip(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / '资料'
            source.mkdir()
            item = source / 'notes.txt'
            item.write_bytes(b'original')
            def update(**values):
                if values.get('status') == 'verifying':
                    item.write_bytes(b'changed after packing')
            destination = root / 'backup.zip'
            with self.assertRaisesRegex(ValueError, '发生变化'):
                build_backup([BackupSource(source, '资料库')], destination, update)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix('.partial').exists())

    def test_outside_symlink_is_preserved_without_copying_its_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / '资料'
            source.mkdir()
            (source / 'outside').symlink_to(root / 'missing-target')
            destination = root / 'backup.zip'
            build_backup([BackupSource(source, '资料库')], destination, lambda **kw: None)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.read('资料库/outside').decode(), str(root / 'missing-target'))
                self.assertEqual(json.loads(archive.read('备份清单.json'))['files'][0]['kind'], 'symlink')


class BackupAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_download_busy_failure_cleanup_and_guard(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'data.txt').write_text('saved content')
            manager = BackupManager()
            service = FastAPI()
            def idle():
                if manager.busy:
                    raise HTTPException(409, 'busy')
            service.include_router(backup_router(manager, lambda: [BackupSource(root / 'data.txt', '资料库/data.txt')], idle))
            entered, release = threading.Event(), threading.Event()
            original = build_backup
            def blocked(*args, **kwargs):
                entered.set()
                release.wait(5)
                return original(*args, **kwargs)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://test') as client:
                    self.assertEqual((await client.post('/api/library/backup')).status_code, 403)
                    with patch('qwen_asr_app.library_backup.build_backup', side_effect=blocked):
                        started = await client.post('/api/library/backup', headers={'X-QwenASR-Client':'library-backup'})
                        self.assertEqual(started.status_code, 202)
                        ident = started.json()['backup']['id']
                        self.assertNotIn('path', started.json()['backup'])
                        await asyncio.to_thread(entered.wait, 5)
                        self.assertTrue(manager.busy)
                        self.assertEqual((await client.get(f'/api/library/backup/{ident}/download')).status_code, 409)
                        self.assertEqual((await client.post('/api/library/backup', headers={'X-QwenASR-Client':'library-backup'})).status_code, 409)
                        release.set()
                        await manager.task
                    done = (await client.get('/api/library/backup')).json()['backup']
                    self.assertEqual(done['status'], 'completed')
                    self.assertEqual(done['percent'], 100)
                    download = await client.get(done['download_url'])
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(download.headers['cache-control'], 'no-store')
                    self.assertEqual(manager.downloads, 0)
                    self.assertEqual(manager.public()['status'], 'released')
                    self.assertFalse(Path(manager.job['path']).exists())
                    self.assertEqual((await client.get(done['download_url'])).status_code, 404)
                    old_directory = manager.directory
                    with patch('qwen_asr_app.library_backup.build_backup', side_effect=OSError('disk failed')):
                        await client.post('/api/library/backup', headers={'X-QwenASR-Client':'library-backup'})
                        await manager.task
                    self.assertFalse(old_directory.exists())
                    self.assertEqual(manager.public()['status'], 'failed')
                    self.assertFalse(manager.busy)
                    self.assertEqual((await client.get(f'/api/library/backup/{ident}')).status_code, 404)
            finally:
                release.set()
                await manager.close()
            self.assertFalse(manager.directory.exists())

    async def test_real_service_rejects_mutations_and_live_clients_during_backup(self):
        with patch.object(app.backup_manager, 'busy', True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                response = await client.put('/api/notes/settings', json={'prompt_template':'do not save'})
                self.assertEqual(response.status_code, 409)
                self.assertEqual((await client.get('/api/library/backup')).status_code, 200)
            with self.assertRaises(HTTPException):
                app.require_backup_idle()
        with patch.object(app.model_manager, 'connected_clients', 1):
            with self.assertRaises(HTTPException):
                app.require_backup_idle()
        with patch.object(app, 'service_mutations', 2):
            with self.assertRaises(HTTPException):
                app.require_backup_idle()
