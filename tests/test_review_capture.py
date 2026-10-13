"""Production config + actual ASGI/WebSocket routes; no models or user data."""
from tests import isolation
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import app
from qwen_asr_app import storage
from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.inference_providers import inference_settings
from qwen_asr_app.settings_recovery import SettingsRecoveryError, backup_path, recover


class SettingsPreservationTests(unittest.TestCase):
    def test_bad_json_structure_and_read_failure_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); path = root / 'settings.json'
            with patch.object(storage, 'SETTINGS_PATH', path), patch.object(storage, 'DEFAULT_STORAGE_ROOT', root / 'library'):
                settings = storage.AppSettings()
                saved = path.read_bytes()
                self.assertTrue(backup_path(path).exists())
                for raw in (b'{"storage_root":', b'[]', b'{}', b'{"storage_root":"relative"}',
                            json.dumps({'storage_root': str(root), 'known_roots': 'bad'}).encode(),
                            json.dumps({'storage_root':str(root),'category_profiles':{'课程':{'asr_params':'broken'}}}).encode(),
                            json.dumps({'storage_root':str(root),'library_layout':[]}).encode()):
                    path.write_bytes(raw)
                    with self.assertRaises(SettingsRecoveryError): storage.AppSettings()
                    self.assertEqual(path.read_bytes(), raw)
                    with self.assertRaises(SettingsRecoveryError): settings.save()
                    self.assertEqual(path.read_bytes(), raw)
                path.write_bytes(saved)
                with patch.object(Path, 'read_bytes', side_effect=PermissionError('temporary')):
                    with self.assertRaisesRegex(SettingsRecoveryError, '暂时'): storage.AppSettings()
                self.assertEqual(path.read_bytes(), saved)
                path.write_text('{broken')
                recover(path)
                self.assertEqual(json.loads(path.read_text())['storage_root'], str(root/'library'))
                self.assertTrue(list(root.glob('settings.json.recovery-*')))
                path.unlink()
                with self.assertRaises(SettingsRecoveryError): storage.AppSettings()
                self.assertFalse(path.exists())
                recover(path)
                path.write_text('{broken-again')
                alternative = root/'existing'; alternative.mkdir()
                recover(path, alternative.resolve())
                self.assertEqual(storage.AppSettings().storage_root, alternative.resolve())

    def test_atomic_write_failure_keeps_last_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); path=root/'settings.json'
            with patch.object(storage,'SETTINGS_PATH',path), patch.object(storage,'DEFAULT_STORAGE_ROOT',root/'library'):
                settings=storage.AppSettings(); before=path.read_bytes()
                from qwen_asr_app import durable
                real_replace=durable.os.replace
                def fail(source, target):
                    if Path(target)==path: raise OSError('synthetic disk full')
                    return real_replace(source,target)
                with patch.object(durable.os,'replace',side_effect=fail):
                    with self.assertRaises(OSError): settings.save()
                self.assertEqual(path.read_bytes(),before)
                self.assertEqual(json.loads(backup_path(path).read_text()),json.loads(before))
                original=settings.storage_root
                with patch.object(durable.os,'replace',side_effect=fail):
                    with self.assertRaises(OSError): settings.set_storage_root(str(root/'other'))
                self.assertEqual(settings.storage_root,original)


class ProductionCaptureTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {'QWEN_ASR_TESTING':'0'}))
        self.stack.enter_context(patch.object(storage,'SETTINGS_PATH',self.root/'settings.json'))
        self.stack.enter_context(patch.object(storage,'DEFAULT_STORAGE_ROOT',self.root/'library'))
        settings=storage.AppSettings()
        for module in (storage,app): self.stack.enter_context(patch.object(module,'app_settings',settings))
        self.stack.enter_context(patch.object(storage,'LEGACY_RECORDINGS_DIR',self.root/'legacy'))
        self.store=storage.RecordingStore()
        self.prefs=AIPreferences(self.root/'ai.json')
        manager=SimpleNamespace(status='ready',load=AsyncMock(),transcribe=AsyncMock(return_value=('合成文字','Chinese',.01)),
                                client_connected=lambda:None,client_disconnected=lambda:None)
        for name,value in [('recording_store',self.store),('ai_preferences',self.prefs),('model_manager',manager),
                           ('live_sessions',{}),('finalization_tasks',{}),('background_summary_tasks',{})]:
            self.stack.enter_context(patch.object(app,name,value))
        self.stack.enter_context(patch.object(inference_settings,'path',self.root/'inference.json'))
        # Audio encoding uses only the synthetic samples created below.
        @asynccontextmanager
        async def no_compute_lifespan(_app):
            yield
        self.stack.enter_context(patch.object(app.app.router,'lifespan_context',no_compute_lifespan))
        self.client=self.stack.enter_context(TestClient(app.app,base_url='http://127.0.0.1:8765'))

    def socket(self):
        return self.client.websocket_connect('ws://127.0.0.1:8765/ws/transcribe', headers={'origin':'http://127.0.0.1:8765'})

    def until(self, ws, kind):
        for _ in range(30):
            event=ws.receive_json()
            if event['type'].endswith('_error') and event['type'] != kind: self.fail(str(event))
            if event['type']==kind: return event
        self.fail('expected '+kind)

    def start(self, ws):
        ws.send_json({'type':'config','title':'合成课堂','category':'未分类'})
        self.until(ws,'configured')
        ws.send_json({'type':'start'})
        return self.until(ws,'session_started')['session']

    def test_first_install_clear_corrupt_and_disabled_save(self):
        for config in (None, '{broken', json.dumps({'ai_correction':False,'summary_enabled':False,'function_providers':{}})):
            with self.subTest(config=config):
                if config is None: self.prefs.clear()
                else: self.prefs.path.write_text(config)
                with self.socket() as ws:
                    self.until(ws,'ready')
                    ws.send_json({'type':'config','ai_correction':True,'ai':{'model':'untrusted-browser'}})
                    self.until(ws,'configured')
                    record=self.start(ws)
                    self.assertFalse(app.live_sessions[record['id']].config['ai_correction'])
                    ws.send_bytes(bytes(1600*4)); ws.send_json({'type':'pause'})
                    self.until(ws,'paused'); self.start(ws)
                    ws.send_json({'type':'end'})
                    self.until(ws,'ended')
                    ws.send_json({'type':'ping'}); self.until(ws,'pong')
                    saved=self.store.find(record['id'])
                    self.assertEqual(saved.metadata['status'],'completed')
                    self.assertTrue(saved.audio_path.is_file())
                    self.assertGreater(saved.audio_path.stat().st_size,0)
                    self.assertTrue(saved.transcript_json_path.is_file())
                    self.assertFalse(saved.working_audio_path.exists())
                    self.assertEqual(saved.metadata['finalization']['status'],'completed')
                    self.assertEqual(saved.metadata['summary_job']['status'],'failed')
                    self.assertFalse(app.finalization_tasks)

    def test_final_flags_validate_only_enabled_providers(self):
        self.prefs.path.write_text(json.dumps({'ai_correction':True,'function_providers':{}}))
        with self.socket() as ws:
            self.until(ws,'ready'); ws.send_json({'type':'config','ai_correction':False})
            self.assertIn('sentence',self.until(ws,'config_error')['message'])
            ws.send_json({'type':'start'})
            self.until(ws,'start_error')
        self.prefs.path.write_text(json.dumps({'summary_enabled':True,'function_providers':{'review':{'transport':'agy_cli','model':'synthetic'}}}))
        result=self.prefs.task_config({'ai_correction':True},operation='live')
        self.assertFalse(result['ai_correction'])
        self.assertTrue(result['summary_enabled'])

    def test_summary_outage_does_not_change_successful_archive(self):
        self.prefs.path.write_text(json.dumps({'function_providers':{'summary':{'transport':'agy_cli','model':'synthetic'}}}))
        with patch.object(app,'generate_library_summary',AsyncMock(side_effect=RuntimeError('synthetic summary unavailable'))):
            with self.socket() as ws:
                self.until(ws,'ready'); record=self.start(ws)
                ws.send_json({'type':'end'}); self.until(ws,'ended')
                for _ in range(30):
                    saved=self.store.find(record['id'])
                    if saved.metadata['summary_job']['status']=='failed':break
                    time.sleep(.01)
                self.assertEqual(saved.metadata['finalization']['status'],'completed')
                self.assertEqual(saved.metadata['status'],'completed')
                self.assertEqual(saved.metadata['summary_job']['status'],'failed')
                self.assertIn('synthetic summary unavailable',saved.metadata['summary_job']['error'])
                self.assertEqual(app._saved_ai_task_config(saved,'summary')['ai']['summary_provider']['model'],'synthetic')

    def test_hung_live_ai_is_paused_without_blocking_archive(self):
        import asyncio
        import threading
        import numpy as np
        entered=threading.Event()
        async def hung(*args,**kwargs):
            entered.set();await asyncio.Event().wait()
        self.prefs.path.write_text(json.dumps({'ai_correction':True,'function_providers':{
            mode:{'transport':'agy_cli','model':'synthetic'} for mode in ('sentence','review')}}))
        record,_=self.store.start_or_resume('合成 AI 卡住','未分类')
        record.append_audio(np.zeros(16000,dtype=np.float32))
        record.metadata['pending_segments']={'1':{'id':1,'start':0,'end':1}}
        record.metadata['next_segment_id']=2;record.save_all()
        with patch('qwen_asr_app.live_session.polish_and_translate',side_effect=hung):
            with self.socket() as ws:
                self.until(ws,'ready');self.start(ws)
                self.assertTrue(entered.wait(1))
                ws.send_json({'type':'end'});self.until(ws,'ended')
                saved=self.store.find(record.id)
                self.assertEqual(saved.metadata['status'],'completed')
                self.assertEqual(saved.segments[0]['raw'],'合成文字')
                self.assertFalse(saved.segments[0]['ai_pending'])
                self.assertEqual(saved.metadata['ai_jobs']['refinement']['status'],'paused')

    def test_pending_asr_recovery_runs_through_real_socket(self):
        record,_=self.store.start_or_resume('合成待恢复','未分类')
        import numpy as np
        record.append_audio(np.zeros(16000,dtype=np.float32))
        record.metadata['pending_segments']={'1':{'id':1,'start':0,'end':1}}
        record.metadata['next_segment_id']=2;record.save_all()
        with self.socket() as ws:
            self.until(ws,'ready'); restored=self.start(ws)
            self.assertEqual(restored['id'],record.id)
            ws.send_json({'type':'end'});self.until(ws,'ended')
            saved=self.store.find(record.id)
            self.assertFalse(saved.metadata['pending_segments'])
            self.assertEqual(saved.segments[0]['raw'],'合成文字')

    def test_simultaneous_start_has_one_winner(self):
        with self.socket() as a,self.socket() as b:
            for ws in (a,b):
                self.until(ws,'ready');ws.send_json({'type':'config','title':'合成并发','category':'未分类'})
                self.until(ws,'configured')
            a.send_json({'type':'start'});b.send_json({'type':'start'})
            first=a.receive_json();second=b.receive_json()
            self.assertEqual(sorted([first['type'],second['type']]),['session_started','start_error'])
            for ws in (a,b):
                ws.send_bytes(bytes(1600*4));ws.send_json({'type':'ping'});self.until(ws,'pong')
            self.assertEqual(self.store.active.metadata['sample_count'],1600)

    def test_preopened_tabs_takeover_disconnect_and_reconnect(self):
        with self.socket() as a, self.socket() as b:
            self.until(a,'ready'); self.until(b,'ready')
            record=self.start(a)
            b.send_json({'type':'start'}); self.assertEqual(self.until(b,'start_error')['code'],'writer_conflict')
            for ws in (a,b):
                ws.send_bytes(bytes(1600*4)); ws.send_json({'type':'ping'}); self.until(ws,'pong')
            self.assertEqual(self.store.active.metadata['sample_count'],1600)
            with self.socket() as observer:
                self.until(observer,'ready')
                self.assertEqual(self.client.get('/api/session').json()['session']['id'],record['id'])
                observer.send_bytes(bytes(1600*4))
                observer.send_json({'type':'pause'}); self.until(observer,'control_error')
                self.assertTrue(app.live_sessions[record['id']].accepting_audio)
            b.send_json({'type':'start','takeover':True}); self.until(b,'session_started'); self.until(a,'ownership_lost')
            a.send_bytes(bytes(1600*4)); a.send_json({'type':'ping'}); self.until(a,'pong')
            b.send_bytes(bytes(1600*4)); b.send_json({'type':'ping'}); self.until(b,'pong')
            self.assertEqual(self.store.active.metadata['sample_count'],3200)
            a.close(); time.sleep(.02)
            self.assertTrue(app.live_sessions[record['id']].accepting_audio)
            b.send_json({'type':'pause'}); self.until(b,'paused')
            canonical=app.live_sessions[record['id']]
            async def pending_work():
                import asyncio
                gate=asyncio.Event()
                canonical.track(gate.wait())
                return gate
            pending=self.client.portal.call(pending_work)
        with self.socket() as c:
            self.until(c,'ready'); restored=self.start(c)
            self.assertEqual(restored['id'],record['id'])
            self.assertIs(app.live_sessions[record['id']],canonical)
            self.assertTrue(canonical.tasks)
            self.client.portal.call(pending.set)
            c.send_json({'type':'end'}); self.until(c,'ended')
