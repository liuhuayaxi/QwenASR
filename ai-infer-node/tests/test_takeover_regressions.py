"""Regressions for independently found takeover defects, no model inference."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from fake_scope import FakeScope, config as synthetic_config
from unittest.mock import AsyncMock, Mock, patch
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client.document_quality import annotate_export, MARKER
from server import resources, cache
m=importlib.import_module('server.app')

class TakeoverLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_process_observation_quarantines_even_when_gpu_is_low(self):
        class Process:
            pid=99999999
            returncode=0
            async def wait(self): return 0
        with tempfile.TemporaryDirectory() as raw:
            cfg={'server':{'data_dir':raw,'job_timeout_seconds':30,'reclaim_model_file_cache':False},'asr':{'python':sys.executable},'mineru':{'python':sys.executable}}
            with patch.dict(os.environ,AI_INFER_TOKEN='x'*48):node=m.Gateway(synthetic_config(cfg))
            try:
                ident='a'*32;node.store.add(ident,ident,'asr',ident,{},'.wav')
                result=Path(raw)/ident/'result';result.mkdir(parents=True)
                (result/'metadata.json').write_text('{}');(result/'result.zip').write_bytes(b'fixture')
                with patch.object(m, 'verify_model_manifests', return_value={'synthetic': True}),patch.object(m, 'TaskScope', FakeScope),patch.object(m,'gpu_usage',AsyncMock(return_value={'used_mib':43})),patch.object(m.asyncio,'create_subprocess_exec',AsyncMock(return_value=Process())),patch.object(m,'kill_group'),patch.object(FakeScope,'observation',side_effect=OSError('observer failed')):
                    await node.execute(ident)
                job=node.store.get(ident);metrics=json.loads(job['metrics'])
                self.assertEqual(job['status'],'completed')
                self.assertEqual(metrics['gpu_release_state'],'unknown')
                self.assertFalse(metrics['gpu_release_verified'])
                self.assertIsNotNone(node.isolation)
            finally:node.store.db.close();node.instance_lock.close()

class TakeoverArtifactTests(unittest.TestCase):
    def test_denied_tracked_identity_stays_unknown_and_is_never_signaled(self):
        proc=Mock();proc.create_time.side_effect=resources.psutil.AccessDenied(123)
        identity={'pid':123,'create_time':42}
        with patch.object(resources.psutil,'Process',return_value=proc):
            self.assertEqual(resources.tracked_processes([identity]),[{**identity,'state':'unknown'}])
            with self.assertRaises(resources.psutil.AccessDenied):resources.terminate_tracked([identity],9)
        proc.send_signal.assert_not_called()

    def test_denied_status_of_known_group_member_is_not_absence(self):
        proc=Mock(pid=123);proc.status.side_effect=resources.psutil.AccessDenied(123)
        with patch.object(resources.psutil,'process_iter',return_value=[proc]),patch.object(resources.os,'getpgid',return_value=456):
            self.assertEqual(resources.group_processes(456),[{'pid':123,'rss_bytes':None,'pss_bytes':None,'state':'unknown'}])

    def test_worker_source_change_invalidates_runtime_even_with_same_model_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'workers').mkdir();p=root/'workers/asr_worker.py';p.write_text('first')
            with patch.object(resources,'RUNTIME_SOURCE_ROOT',root):
                first=resources.runtime_identity({'asr':{},'mineru':{}})
                p.write_text('other')
                self.assertNotEqual(first,resources.runtime_identity({'asr':{},'mineru':{}}))

    def test_chart_uncertainty_is_in_consumed_content_and_raw_export_is_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);body='| x | y |\n| --- | --- |\n| 1 | 99 |'
            doc={'pages':[{'page_idx':0,'blocks':[{'type':'chart','content':[{'type':'chart_body','content':body}]}]}]}
            originals={'middle_json.json':json.dumps(doc).encode(),'document_content_list.json':json.dumps([{'type':'chart','content':body,'page_idx':0}]).encode(),'markdown.md':('<details>\n<summary>line</summary>\n\n'+body+'\n</details>').encode()}
            for name,content in originals.items():(root/name).write_bytes(content)
            annotate_export(root)
            for name in originals:self.assertIn(MARKER,(root/name).read_text())
            with zipfile.ZipFile(root/'raw-mineru.zip') as z:
                for name,content in originals.items():self.assertEqual(content,z.read(name))
            archive=(root/'raw-mineru.zip').read_bytes();annotate_export(root)
            self.assertEqual(archive,(root/'raw-mineru.zip').read_bytes())
            self.assertIn('99',(root/'middle_json.json').read_text())

    @unittest.skipUnless(hasattr(os,'posix_fadvise'),'Linux file advice integration')
    def test_reclaim_private_venv_library_but_not_external_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);env=root/'venv';(env/'bin').mkdir(parents=True);(env/'lib').mkdir();(env/'pyvenv.cfg').write_text('home=/usr/bin')
            owned=env/'lib/libtest.so';external=root/'libexternal.so'
            for path in (owned,external):
                with path.open('wb') as f:f.truncate(5*1024**2)
            (env/'lib/liblink.so').symlink_to(external)
            cfg={'server':{},'asr':{'python':str(env/'bin/python')},'mineru':{}}
            with patch.object(os,'posix_fadvise') as advice,patch.object(cache,'resident_bytes',return_value=0):
                value=cache.reclaim_model_cache(cfg)
            self.assertEqual(advice.call_count,0)
            self.assertNotIn('runtime_libraries', value['by_scope'])
            self.assertFalse(value['bytes_advised_is_reclaimed_bytes'])
