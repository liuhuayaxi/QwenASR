from tests import isolation  # noqa: F401

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI

from qwen_asr_app.drive_backup import (API, UPLOAD, TOKEN, SCOPE, CHUNK, DEFAULTS,
    DriveBackup, DriveError, drive_backup_router, inventory, snapshot)


class FakeDrive:
    def __init__(self):
        self.files = {}
        self.sessions = {}
        self.counter = 0
        self.uploads = 0
        self.lose_chunk = False
        self.lose_final = False
        self.corrupt = False
        self.invalid_grant = False
        self.denied_file = None
        self.requests = []

    def __call__(self, req):
        self.requests.append((req.method, req.url.path))
        path = req.url.path
        if str(req.url) == TOKEN:
            if self.invalid_grant:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                                            "scope": SCOPE, "expires_in": 3600})
        if path.endswith('/about'):
            return httpx.Response(200, json={"user": {"displayName": "Synthetic", "emailAddress": "test@example.invalid", "permissionId": "account1"}})
        if path.endswith('/generateIds'):
            self.counter += 1
            return httpx.Response(200, json={"ids": [f"id{self.counter}"]})
        if path.startswith('/upload/'):
            session_id = req.url.params.get('upload_id')
            if not session_id:
                meta = json.loads(req.content)
                ident = path.rsplit('/', 1)[-1] if req.method == 'PATCH' else meta['id']
                if req.method == 'POST' and ident in self.files:
                    return httpx.Response(409)
                self.counter += 1
                session_id = f"session{self.counter}"
                self.sessions[session_id] = {"id": ident, "data": b'', "metadata": meta,
                                            "total": int(req.headers['X-Upload-Content-Length'])}
                return httpx.Response(200, headers={"Location": UPLOAD + '?upload_id=' + session_id})
            session = self.sessions.get(session_id)
            if not session:
                return httpx.Response(404)
            if session.get('done'):
                return httpx.Response(200, json=self.files[session['id']])
            if req.content:
                start = int(req.headers['content-range'].split(' ')[1].split('-')[0])
                assert start == len(session['data']), (start, len(session['data']))
                session['data'] += req.content
                if self.lose_chunk and len(session['data']) < session['total']:
                    self.lose_chunk = False
                    raise httpx.ReadError('synthetic lost chunk response')
            if len(session['data']) == session['total']:
                session['done'] = True
                self.uploads += 1
                self.files[session['id']] = {**self.files.get(session['id'], {}), **session['metadata'],
                    "id": session['id'], "size": str(len(session['data'])), "trashed": False,
                    "md5Checksum": 'incorrect' if self.corrupt else hashlib.md5(session['data']).hexdigest()}
                if self.lose_final:
                    self.lose_final = False
                    raise httpx.ReadError('synthetic lost completion response')
                return httpx.Response(200, json=self.files[session['id']])
            headers = {"Range": f"bytes=0-{len(session['data'])-1}"} if session['data'] else {}
            return httpx.Response(308, headers=headers)
        if req.method == 'POST' and path.endswith('/files'):
            meta = json.loads(req.content)
            if meta['id'] in self.files:
                return httpx.Response(409)
            self.files[meta['id']] = {**meta, "trashed": False}
            return httpx.Response(200, json=self.files[meta['id']])
        if req.method == 'GET' and '/files/' in path:
            ident = path.rsplit('/', 1)[-1]
            if self.denied_file == ident:
                return httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientFilePermissions"}]}})
            value = self.files.get(ident)
            return httpx.Response(200, json=value) if value else httpx.Response(404)
        raise AssertionError(f'Unexpected mock request: {req.method} {path}')


class DriveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.settings = SimpleNamespace(storage_root=self.library)
        self.records = SimpleNamespace(list_records=lambda: [])
        self.notes = SimpleNamespace(notes=lambda: [])
        self.fake = FakeDrive()
        self.manager = self.make_manager()

    def make_manager(self):
        return DriveBackup(self.settings, self.records, self.notes, directory=self.root / 'private',
                           transport=httpx.MockTransport(self.fake))

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    def file(self, relative, data=b'synthetic document'):
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def connect(self):
        self.manager.state.update(client={"client_id": "test.apps.googleusercontent.com", "client_secret": "synthetic-secret"},
            tokens={"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_at": time.time() + 3600},
            account_id="account1", accounts={"account1": {"files": {}, "folders": {}}})
        self.manager.save()

    async def run_job(self, sample=False):
        self.manager.start(sample=sample)
        await self.manager.task
        return self.manager.public()['job']

    async def test_document_inventory_excludes_secrets_audio_links_and_optional_notes(self):
        for name in ['课程/讲义.pdf', '课程/上课录音/一/transcript.txt', '课程/上课录音/一/transcript.json',
                     '课程/上课录音/一/AI修正后文本.txt', '课程/AI生成笔记/知识.md', '课程/上课录音/一/AI总结.md',
                     'private/secret.pdf', '.history/old.pdf', 'backups/archive.pdf', '课程/session.json',
                     '课程/secret.json', '课程/recording.m4a', '课程/api_key.txt', '课程/.cache.pdf']:
            self.file(name)
        outside = self.root / 'outside.pdf'
        outside.write_bytes(b'never upload linked data')
        (self.library / 'linked.pdf').symlink_to(outside)
        (self.library / 'linked-folder').symlink_to(outside.parent, target_is_directory=True)
        found = inventory(self.settings, self.records, self.notes, DEFAULTS)
        self.assertEqual(len(found), 4)
        self.assertTrue(all(x['relative'].startswith('资料库/课程/') for x in found))
        options = {**DEFAULTS, 'notes': True}
        self.assertEqual(len(inventory(self.settings, self.records, self.notes, options)), 6)
        result = await self.manager.preview()
        self.assertEqual(result['preview']['count'], 4)
        self.assertEqual(self.fake.requests, [])

    async def test_snapshot_rejects_change_or_link_swap(self):
        source = self.file('lecture.pdf')
        item = inventory(self.settings, self.records, self.notes, DEFAULTS)[0]
        source.write_bytes(b'changed')
        with self.assertRaises(DriveError):
            snapshot(item, self.root / 'copy')
        item = inventory(self.settings, self.records, self.notes, DEFAULTS)[0]
        source.unlink()
        source.symlink_to(self.root / 'elsewhere')
        with self.assertRaises(DriveError):
            snapshot(item, self.root / 'copy')

    async def test_incremental_update_cloud_verification_and_no_delete(self):
        self.connect()
        path = self.file('课程/讲义.pdf')
        self.file('课程/transcript.txt', b'transcript')
        job = await self.run_job()
        self.assertEqual((job['status'], job['uploaded']), ('completed', 2), job)
        self.assertTrue(self.manager.public()['folder_url'].startswith('https://drive.google.com/drive/folders/'))
        ids = set(self.fake.files)
        job = await self.run_job()
        self.assertEqual(job['skipped'], 2)
        self.assertEqual(self.fake.uploads, 2)
        path.write_bytes(b'new contents')
        job = await self.run_job()
        self.assertEqual((job['uploaded'], job['skipped']), (1, 1))
        self.assertEqual(set(self.fake.files), ids)
        path.unlink()
        await self.run_job()
        self.assertEqual(set(self.fake.files), ids)
        self.assertFalse(any(method == 'DELETE' for method, _ in self.fake.requests))
        public = json.dumps(self.manager.public())
        for secret in ('synthetic-access', 'synthetic-refresh', 'synthetic-secret', 'upload_id'):
            self.assertNotIn(secret, public)
        self.assertEqual(self.manager.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.directory.stat().st_mode & 0o777, 0o700)

    async def test_lost_chunk_and_completion_resume_without_duplicates(self):
        self.connect()
        self.file('big.pdf', b'x' * (CHUNK + 11))
        self.fake.lose_chunk = self.fake.lose_final = True
        with patch('qwen_asr_app.drive_backup.asyncio.sleep', new=AsyncMock()):
            job = await self.run_job()
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(self.fake.uploads, 1)
        documents = [x for x in self.fake.files.values() if 'size' in x]
        self.assertEqual(len(documents), 1)

    async def test_bad_checksum_is_failure_and_can_be_retried(self):
        self.connect()
        self.file('lecture.pdf')
        self.fake.corrupt = True
        job = await self.run_job()
        self.assertEqual((job['status'], job['failed']), ('partial', 1))
        self.assertIsNone(self.manager.public()['last_success'])
        self.fake.corrupt = False
        job = await self.run_job()
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(len([x for x in self.fake.files.values() if 'size' in x]), 1)

    async def test_small_test_and_restart_reuse_checkpoint(self):
        self.connect()
        for number in range(5):
            self.file(f'{number}.pdf', b'x' * (number + 1))
        job = await self.run_job(sample=True)
        self.assertEqual(job['total'], 3)
        self.assertIsNone(self.manager.public()['last_success'])
        self.manager = self.make_manager()
        job = await self.run_job()
        self.assertEqual((job['skipped'], job['uploaded']), (3, 2))

    async def test_persisted_partial_session_resumes_after_service_restart(self):
        self.connect()
        self.file('large.pdf', b'a' * (CHUNK + 24))
        original = self.fake.__call__
        broken = True
        def interrupted(req):
            nonlocal broken
            result = original(req)
            if broken and req.method == 'PUT' and len(req.content) == CHUNK:
                # First chunk reaches Drive, then the process loses its connection.
                broken = False
                self.manager.stop_requested = True
                raise httpx.ReadError('synthetic network loss')
            return result
        self.manager.transport = httpx.MockTransport(interrupted)
        with patch('qwen_asr_app.drive_backup.asyncio.sleep', new=AsyncMock()):
            job = await self.run_job()
        self.assertEqual(job['status'], 'stopped')
        entry = next(iter(self.manager.state['accounts']['account1']['files'].values()))
        old_id, session = entry['id'], entry['session']
        self.assertEqual(len(next(iter(self.fake.sessions.values()))['data']), CHUNK)
        self.manager = self.make_manager()
        job = await self.run_job()
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(self.fake.uploads, 1)
        entry = next(iter(self.manager.state['accounts']['account1']['files'].values()))
        self.assertEqual(entry['id'], old_id)
        self.assertNotIn('session', entry)

    async def test_deleted_remote_file_is_recreated_with_a_new_id(self):
        self.connect()
        self.file('lecture.pdf')
        await self.run_job()
        entry = next(iter(self.manager.state['accounts']['account1']['files'].values()))
        old_id = entry['id']
        del self.fake.files[old_id]
        job = await self.run_job()
        self.assertEqual(job['status'], 'completed', job)
        self.assertNotEqual(entry['id'], old_id)

    async def test_invalid_settings_do_not_replace_existing_account(self):
        self.connect()
        before = json.dumps(self.manager.state, sort_keys=True)
        with self.assertRaises(DriveError):
            self.manager.configure({'credentials': {'installed': {'client_id': 'new.apps.googleusercontent.com', 'client_secret': 'new-secret'}},
                                    'options': {'pdf': False, 'transcripts': False}})
        self.assertEqual(json.dumps(self.manager.state, sort_keys=True), before)

    async def test_expired_grant_is_actionable_without_token_leak(self):
        self.connect()
        self.file('lecture.pdf')
        self.manager.state['tokens']['expires_at'] = 0
        self.fake.invalid_grant = True
        job = await self.run_job()
        self.assertFalse(self.manager.public()['connected'])
        self.assertIn('重新连接', self.manager.public()['error'])
        self.assertNotIn('synthetic-refresh', json.dumps(job))

    async def test_authorization_pkce_state_consumption_and_account_binding(self):
        self.manager.configure({'credentials': {'installed': {'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'synthetic-secret'}}})
        server = SimpleNamespace(sockets=[SimpleNamespace(getsockname=lambda: ('127.0.0.1', 12345))], close=lambda: None,
                                 wait_closed=AsyncMock())
        with patch('qwen_asr_app.drive_backup.asyncio.start_server', new=AsyncMock(return_value=server)):
            url = await self.manager.begin_auth()
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertEqual(params['scope'], [SCOPE])
        self.assertNotIn('synthetic-secret', url)
        class Writer:
            payload = b''
            def write(self, value): self.payload += value
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass
        async def callback(state):
            reader = asyncio.StreamReader()
            reader.feed_data(f'GET /?state={state}&code=synthetic-code HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n'.encode())
            writer = Writer()
            await self.manager.callback(reader, writer)
            return writer.payload
        wrong = await callback('wrong-state')
        self.assertIn(b'400 Bad Request', wrong)
        self.assertTrue(self.manager.pending_auth)
        good = await callback(params['state'][0])
        self.assertIn(b'200 OK', good)
        self.assertTrue(self.manager.public()['connected'])
        self.assertIsNone(self.manager.pending_auth)
        self.assertEqual(self.manager.state['account_id'], 'account1')
        replay = await callback(params['state'][0])
        self.assertIn(b'400 Bad Request', replay)
        self.assertNotIn(b'synthetic-code', good)

    async def test_api_guard_options_and_disconnect_preserve_remote(self):
        service = FastAPI()
        service.include_router(drive_backup_router(self.manager))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://test') as client:
            self.assertEqual((await client.post('/api/library/drive/start')).status_code, 403)
            headers = {'X-QwenASR-Client': 'drive-backup'}
            self.assertEqual((await client.post('/api/library/drive/preview', headers={**headers, 'Origin':'https://evil.invalid'})).status_code, 403)
            response = await client.post('/api/library/drive/configure', headers=headers, json={'options':{'pdf':False, 'transcripts':False}})
            self.assertEqual(response.status_code, 400)
            response = await client.post('/api/library/drive/configure', headers=headers, json={'credentials':{'web':{}}})
            self.assertEqual(response.status_code, 400)
            self.connect()
            self.file('lecture.pdf')
            response = await client.post('/api/library/drive/start', headers=headers)
            self.assertTrue(response.json()['busy'])
            await self.manager.task
            ids = set(self.fake.files)
            response = await client.post('/api/library/drive/disconnect', headers=headers)
            self.assertFalse(response.json()['connected'])
            self.assertEqual(set(self.fake.files), ids)
            self.assertNotIn('tokens', self.manager.state)

    async def test_manual_stop_disables_automatic_and_maintenance_blocks_start(self):
        self.connect()
        self.manager.state['options']['automatic'] = True
        await self.manager.stop()
        self.assertFalse(self.manager.state['options']['automatic'])
        self.manager.can_run = lambda: False
        with self.assertRaises(DriveError):
            self.manager.start()

    async def test_corrupt_checkpoint_is_preserved_and_never_overwritten(self):
        self.manager.directory.mkdir()
        self.manager.path.write_text('{broken')
        self.manager = self.make_manager()
        self.assertIn('无法读取', self.manager.public()['error'])
        with self.assertRaises(DriveError):
            self.manager.configure({'options': {'notes': True}})
        self.assertEqual(self.manager.path.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
