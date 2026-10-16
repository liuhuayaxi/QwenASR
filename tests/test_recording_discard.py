from tests import isolation
import asyncio
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

from qwen_asr_app import storage
from qwen_asr_app.durable import atomic_json
from qwen_asr_app.recording_discard import RecordingDiscard,finish_owned


class Record:
    def __init__(self,root,ident,status='active'):
        self.id=ident;self.directory=root/ident;self.directory.mkdir()
        self.metadata={'id':ident,'kind':'microphone','status':status,'title':ident,
                       'ai_note_summary':{'summary':'手动笔记，保留原文'}}
        self.audio_path=self.directory/'audio.aac'
        self.working_audio_path=self.directory/'audio.pcm.wav'
        self.meta_path=self.directory/'session.json'
        with wave.open(str(self.working_audio_path),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\0\0'*160)
        self.save_all()
    def save_metadata(self):atomic_json(self.meta_path,self.metadata)
    def save_all(self):
        self.save_metadata();(self.directory/'AI总结.md').write_text(self.metadata['ai_note_summary']['summary'])


class Store:
    delete=storage.RecordingStore.delete
    def __init__(self,records):
        self.records={r.id:r for r in records};self.paths={r.id:r.directory for r in records};self.active=records[0]
    def find(self,ident):
        record=self.records.get(ident)
        return record if record and record.directory.is_dir() else None


class RecordingDiscardTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalizing_writer_drains_before_real_trash_move_and_new_record_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a=Record(root,'target-a','processing');b=Record(root,'newer-b')
            a.metadata['finalization']={'status':'running'};a.save_metadata()
            store=Store([a,b]);session=SimpleNamespace(recording=a,accepting_audio=True,writer=object(),send=AsyncMock())
            entered=threading.Event();release=threading.Event()
            def finish_audio():
                entered.set();release.wait(4)
                a.audio_path.write_bytes(b'accepted-aac-output')
            writer=asyncio.create_task(asyncio.to_thread(finish_audio))
            async def quiesce(record,live):await writer
            manager=RecordingDiscard(store,{a.id:session},quiesce,root/'receipts.json')
            with patch.object(storage,'TRASH_ROOT',root/'trash'):
                first=manager.start(a.id);task=manager.tasks[a.id];self.assertEqual(first['status'],'discarding')
                self.assertFalse(session.accepting_audio);self.assertIsNone(session.writer)
                await asyncio.sleep(.02);self.assertTrue(entered.is_set());self.assertTrue(a.directory.exists())
                self.assertEqual(manager.start(a.id)['status'],'discarding');self.assertIs(manager.tasks[a.id],task)
                # A delayed completion must never clear or delete a successor.
                store.active=b;release.set();await manager.close()
                result=manager.snapshot(a.id);target=Path(result['trashed_path'])
                self.assertEqual(result['status'],'discarded');self.assertIs(store.active,b);self.assertTrue(b.directory.exists())
                self.assertEqual((target/'audio.aac').read_bytes(),b'accepted-aac-output')
                self.assertEqual((target/'AI总结.md').read_text(),'手动笔记，保留原文')
                manifest=json.loads((target/'.recording-discard.json').read_text())
                self.assertEqual(manifest['session_id'],a.id)
                aac=next(row for row in manifest['audio'] if row['filename']=='audio.aac')
                self.assertEqual(aac['sha256'],hashlib.sha256(b'accepted-aac-output').hexdigest())
                restored=RecordingDiscard(store,{},AsyncMock(),root/'receipts.json')
                self.assertEqual(restored.snapshot(a.id)['status'],'discarded')
                self.assertEqual(restored.start(a.id)['status'],'discarded')

    async def test_failed_drain_retains_audio_and_can_retry_after_reconfirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=Record(root,'target');store=Store([r]);before=r.working_audio_path.read_bytes()
            manager=RecordingDiscard(store,{},AsyncMock(side_effect=RuntimeError('synthetic owner busy')),root/'receipts.json')
            manager.start(r.id);await manager.close()
            self.assertEqual(manager.snapshot(r.id)['status'],'failed');self.assertEqual(r.working_audio_path.read_bytes(),before)
            manager.quiesce=AsyncMock()
            with patch.object(storage,'TRASH_ROOT',root/'trash'):
                manager.start(r.id);await manager.close();self.assertEqual(manager.snapshot(r.id)['status'],'discarded')

    async def test_disconnected_browser_does_not_cancel_confirmed_discard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=Record(root,'target');store=Store([r]);session=SimpleNamespace(recording=r,send=AsyncMock(side_effect=RuntimeError('closed')))
            manager=RecordingDiscard(store,{r.id:session},AsyncMock(),root/'receipts.json')
            with patch.object(storage,'TRASH_ROOT',root/'trash'):
                manager.start(r.id);await manager.close();self.assertEqual(manager.snapshot(r.id)['status'],'discarded')

    async def test_restart_pending_intent_requires_new_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=Record(root,'target');store=Store([r]);path=root/'receipts.json'
            atomic_json(path,{r.id:{'session_id':r.id,'status':'discarding'}})
            manager=RecordingDiscard(store,{},AsyncMock(),path)
            self.assertEqual(manager.snapshot(r.id)['status'],'failed');self.assertTrue(r.directory.exists());self.assertFalse(manager.tasks)

    async def test_actual_restore_releases_archived_files_and_can_discard_again_with_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=Record(root,'restored-record');store=Store([r])
            session=SimpleNamespace(recording=r,accepting_audio=True,writer=object(),send=AsyncMock())
            manager=RecordingDiscard(store,{r.id:session},AsyncMock(),root/'receipts.json')
            with patch.object(storage,'TRASH_ROOT',root/'trash'):
                manager.start(r.id);await manager.close();first=manager.snapshot(r.id)
                self.assertEqual(first['status'],'discarded')
                self.assertFalse(manager.reconcile_restored(r.id))
                shutil.move(first['trashed_path'],r.directory)
                self.assertTrue(manager.reconcile_restored(r.id))
                restored=manager.snapshot(r.id)
                self.assertEqual(restored['status'],'restored')
                self.assertEqual(restored['history'][0]['operation_id'],first['operation_id'])
                self.assertEqual(r.metadata['discard_job']['status'],'restored')
                self.assertEqual((r.directory/'AI总结.md').read_text(),'手动笔记，保留原文')
                # Restoration never revives the socket that originally owned it.
                self.assertTrue(session.discarding);self.assertFalse(session.accepting_audio)
                self.assertIsNone(session.writer);self.assertIsNone(session.recording)
                self.assertNotIn(r.id,manager.sessions)
                restarted=RecordingDiscard(store,{},AsyncMock(),root/'receipts.json')
                second=restarted.start(r.id)
                self.assertEqual(second['status'],'discarding')
                self.assertNotEqual(second['operation_id'],first['operation_id'])
                await restarted.close();again=restarted.snapshot(r.id)
                self.assertEqual(again['status'],'discarded')
                self.assertEqual(again['history'][0]['operation_id'],first['operation_id'])
                self.assertEqual(again['history'][0]['manifest_sha256'],first['manifest_sha256'])

    async def test_copy_or_changed_manifest_cannot_release_completed_discard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=Record(root,'copied-record');store=Store([r])
            manager=RecordingDiscard(store,{},AsyncMock(),root/'receipts.json')
            with patch.object(storage,'TRASH_ROOT',root/'trash'):
                manager.start(r.id);await manager.close();receipt=manager.snapshot(r.id)
                target=Path(receipt['trashed_path']);shutil.copytree(target,r.directory)
                self.assertFalse(manager.reconcile_restored(r.id))
                self.assertEqual(manager.start(r.id)['status'],'discarded')
                # A genuine move is necessary, and its receipt-bound manifest
                # must still belong to this exact completed operation.
                shutil.rmtree(r.directory);shutil.move(target,r.directory)
                manifest_path=r.directory/'.recording-discard.json';original=manifest_path.read_bytes()
                changed=json.loads(original);changed['operation_id']='another-operation'
                atomic_json(manifest_path,changed)
                self.assertFalse(manager.reconcile_restored(r.id))
                self.assertEqual(r.metadata['discard_job']['status'],'discarded')
                manifest_path.write_bytes(original)
                self.assertTrue(manager.reconcile_restored(r.id))

    async def test_pre_operation_id_receipt_recognizes_matching_original_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=Record(root,'legacy-restored');store=Store([r])
            manager=RecordingDiscard(store,{},AsyncMock(),root/'receipts.json')
            with patch.object(storage,'TRASH_ROOT',root/'trash'):
                manager.start(r.id);await manager.close();receipt=manager.snapshot(r.id)
                for key in ('operation_id','manifest_sha256','original_path','history'):
                    receipt.pop(key,None)
                manager.results[r.id]=receipt;manager.persist()
                target=Path(receipt['trashed_path']);manifest=json.loads((target/'.recording-discard.json').read_text())
                manifest.pop('operation_id');atomic_json(target/'.recording-discard.json',manifest)
                r.metadata['discard_job'].pop('operation_id');r.metadata['discard_job'].pop('original_path');
                shutil.move(target,r.directory);r.save_metadata()
                restarted=RecordingDiscard(store,{},AsyncMock(),root/'receipts.json')
                self.assertEqual(restarted.snapshot(r.id)['status'],'restored')
                self.assertEqual(restarted.snapshot(r.id)['history'][0]['trashed_path'],receipt['trashed_path'])

    async def test_cancelled_waiter_retains_writer_until_it_finishes(self):
        release=asyncio.Event();writer=asyncio.create_task(release.wait());waiter=asyncio.create_task(finish_owned(writer))
        await asyncio.sleep(0);waiter.cancel();await asyncio.sleep(0);waiter.cancel();await asyncio.sleep(0)
        self.assertFalse(waiter.done());self.assertFalse(writer.cancelled());release.set()
        with self.assertRaises(asyncio.CancelledError):await waiter
        self.assertTrue(writer.done())

    async def test_endpoint_requires_confirmation_and_exact_record_id(self):
        import app
        from fastapi import HTTPException
        with patch.object(app,'recording_discard',Mock()) as manager:
            for body in [{'confirmed':False,'expected_session_id':'a'},{'confirmed':True,'expected_session_id':'b'}]:
                with self.assertRaises(HTTPException):await app.discard_recording('a',body)
            manager.start.assert_not_called()


class DiscardHTTPFinalizationTests(unittest.TestCase):
    def test_confirmed_http_discard_waits_for_actual_finalization_writer(self):
        import app,time
        from tests.test_review_capture import ProductionCaptureTests
        harness=ProductionCaptureTests('test_first_install_clear_corrupt_and_disabled_save')
        harness.setUp();self.addCleanup(harness.doCleanups);harness.prefs.clear()
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def finish_encoding(record):
            entered.set()
            if not release.wait(5):raise RuntimeError('synthetic encoder timeout')
            record.audio_path.write_bytes(b'synthetic-accepted-encoded-audio')
            record.metadata['status']='completed';record.save_all()
        manager=RecordingDiscard(harness.store,app.live_sessions,app._quiesce_discarded_recording,harness.root/'discard-receipts.json')
        with patch.object(app,'recording_discard',manager),patch.object(storage,'TRASH_ROOT',harness.root/'trash'),patch.object(storage.RecordingRecord,'complete',finish_encoding):
            with harness.socket() as ws:
                harness.until(ws,'ready');public=harness.start(ws);ident=public['id']
                ws.send_bytes(bytes(1600*4));ws.send_json({'type':'end'})
                harness.until(ws,'ending');self.assertTrue(entered.wait(2))
                record=harness.store.find(ident);original=record.directory
                bad=harness.client.post(f'/api/recordings/{ident}/discard',json={'confirmed':False,'expected_session_id':ident})
                self.assertEqual(bad.status_code,400);self.assertTrue(original.exists())
                response=harness.client.post(f'/api/recordings/{ident}/discard',json={'confirmed':True,'expected_session_id':ident})
                self.assertEqual(response.status_code,202);self.assertTrue(original.exists());self.assertFalse(manager.tasks[ident].done())
                release.set()
                for _ in range(100):
                    result=harness.client.get(f'/api/recordings/{ident}/discard').json()
                    if result['status']!='discarding':break
                    time.sleep(.02)
                self.assertEqual(result['status'],'discarded',result)
                target=Path(result['trashed_path']);self.assertFalse(original.exists())
                self.assertEqual((target/record.audio_path.name).read_bytes(),b'synthetic-accepted-encoded-audio')
                self.assertIsNone(harness.store.active)
                shutil.move(target,original)
                library=harness.client.get('/api/library')
                self.assertEqual(library.status_code,200)
                self.assertEqual(manager.snapshot(ident)['status'],'restored')
                restored=harness.client.get(f'/api/recordings/{ident}')
                self.assertEqual(restored.status_code,200)
                self.assertEqual(restored.json()['session']['discard_job']['status'],'restored')
                self.assertEqual(manager.snapshot(ident)['status'],'restored')


if __name__=='__main__':unittest.main()
