from tests import isolation  # noqa: F401
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import weakref

from qwen_asr_app.storage import RecordingStore,RecordingRecord


class RemoteBacklogIdleTests(unittest.TestCase):
    def test_refresh_cache_is_bounded_during_the_scan_not_only_at_the_end(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);paths=[];alive=weakref.WeakSet();peak=0
            for i in range(30):
                path=folder/str(i)/'session.json';path.parent.mkdir();path.write_text('{}');paths.append(path)
            store=RecordingStore.__new__(RecordingStore);store.active=None;store.paths={};store._read_cache={};store._metadata_paths=lambda:paths
            def load(path):
                nonlocal peak
                record=RecordingRecord(path,{'id':'record-'+path.name,'updated_at':path.name,'status':'completed','kind':'upload'})
                alive.add(record);peak=max(peak,len(alive));return record
            with patch.object(RecordingRecord,'load',side_effect=load):store.refresh()
            # Four cached records, the previous loop-local record and the
            # replacement being loaded are the maximum live objects.
            self.assertLessEqual(peak,6);self.assertLessEqual(len(store._read_cache),4)
            peak=0
            with patch.object(RecordingRecord,'load',side_effect=load):
                self.assertEqual(sum(1 for _ in store.iter_records()),30)
            self.assertLessEqual(peak,6)

    def test_idle_recovery_does_not_load_audio_or_native_modules_or_all_recordings(self):
        code='''import asyncio,sys
from types import SimpleNamespace
from qwen_asr_app.remote_backlog import retry_once
def forbidden():raise AssertionError('full recording scan during idle')
store=SimpleNamespace(remote_backlog_ids=lambda:[],list_records=forbidden)
asyncio.run(retry_once(store,None,{},lambda _:False))
assert not any(name in sys.modules for name in ['soundfile','numpy','pymupdf','fitz','qwen_asr_app.live_session'])
'''
        subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10,check=True)

    def test_header_cache_loads_only_changed_metadata_and_filters_paused_or_local_work(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);paths=[]
            remote={'inference_settings':{'asr_provider':'remote_only'},'_remote_operation_dir':'/synthetic/clip'}
            for ident,status,config in [('remote-test','pending',remote),('paused-test','paused',remote),
                                        ('local-test','pending',{'inference_settings':{'asr_provider':'local'}})]:
                path=folder/ident/'session.json';path.parent.mkdir();paths.append(path)
                path.write_text(json.dumps({'id':ident,'remote_backlog':{'status':status},'pending_segments':{'1':{'config_snapshot':config}},'large_note':'text'*1000}))
                (path.parent/'transcript.json').write_text('must never be read by the idle scan')
            store=RecordingStore.__new__(RecordingStore);store.active=None;store.paths={};store._metadata_paths=lambda:paths
            with patch.object(RecordingRecord,'load',side_effect=AssertionError('transcript loaded')):
                self.assertEqual(store.remote_backlog_ids(),['remote-test'])
                with patch.object(Path,'read_text',side_effect=AssertionError('unchanged metadata reread')):
                    self.assertEqual(store.remote_backlog_ids(),['remote-test'])
                paths[0].write_text(json.dumps({'id':'remote-test','pending_segments':{}}))
                self.assertEqual(store.remote_backlog_ids(),[])
            self.assertTrue(all(len(entry)==3 and isinstance(entry[2],bool) for entry in store._remote_backlog_cache.values()))
