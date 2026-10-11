"""Isolated failure injection, not hardware or host acceptance."""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from fake_scope import FakeScope, config as synthetic_config
from pathlib import Path
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
m = importlib.import_module('server.app')

class Process:
    pid = 99999999
    returncode = 0
    async def wait(self): return 0

class LifecycleRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_monitor_failure_and_cleanup_error_quarantine_next_job(self):
        for case in ('missing_baseline', 'missing_after', 'high_after', 'kill_error'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                cfg={'server':{'data_dir':raw, 'job_timeout_seconds':30, 'gpu_release_timeout_seconds':0, 'reclaim_model_file_cache':False}, 'asr':{'python':sys.executable}, 'mineru':{'python':sys.executable}}
                with patch.dict(os.environ,AI_INFER_TOKEN='x'*48):node=m.Gateway(synthetic_config(cfg))
                for ident in ('a'*32, 'b'*32):
                    node.store.add(ident,ident,'asr',ident,{},'.wav')
                    result=Path(raw)/ident/'result';result.mkdir(parents=True)
                    (result/'metadata.json').write_text('{}');(result/'result.zip').write_bytes(b'fixture')
                gpus=[{'available':False}]*2 if case=='missing_baseline' else [{'used_mib':43}, {'available':False} if case=='missing_after' else {'used_mib':4000 if case=='high_after' else 43}]
                spawn=AsyncMock(return_value=Process())
                with patch.object(m, 'verify_model_manifests', return_value={'synthetic': True}),patch.object(m, 'TaskScope', FakeScope),patch.object(m,'gpu_usage',AsyncMock(side_effect=gpus)),patch.object(m.asyncio,'create_subprocess_exec',spawn),patch.object(FakeScope,'stop',side_effect=PermissionError('injected') if case=='kill_error' else None):
                    await node.execute('a'*32);await node.execute('b'*32)
                job=node.store.get('a'*32);metrics=json.loads(job['metrics'])
                self.assertEqual(job['status'], 'failed' if case == 'missing_baseline' else 'completed');self.assertFalse(metrics['gpu_release_verified'])
                self.assertEqual(spawn.call_count,0 if case=='missing_baseline' else 1)
                self.assertEqual(node.store.get('b'*32)['status'],'queued')
                self.assertIsNone(node.current);self.assertIsNotNone(node.isolation)
                self.assertTrue((node.coordination/'resource-isolation.json').exists())
                node.store.db.close();node.instance_lock.close()

    async def test_recovery_requires_three_gpu_and_owned_tree_observations(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg={'server':{'data_dir':raw}}
            with patch.dict(os.environ,AI_INFER_TOKEN='x'*48):node=m.Gateway(synthetic_config(cfg))
            node.scope_path='/synthetic-scope';node.isolate('unknown',43,99999999)
            node.startup_checks = {'state': 'verified', 'test_double': True}
            with patch.object(m, 'verify_model_manifests', return_value={'synthetic': True}),patch.object(m, 'TaskScope', FakeScope),patch.object(m,'gpu_usage',AsyncMock(return_value={'used_mib':43})),patch.object(m,'group_processes',return_value=[]),patch.object(m,'observe_saved',return_value={'state':'observed','populated':False}):
                await node.reconcile();await node.reconcile();self.assertIsNotNone(node.isolation)
                await node.reconcile();self.assertIsNone(node.isolation)
            node.isolate('unknown',None,None)
            with patch.object(m, 'verify_model_manifests', return_value={'synthetic': True}),patch.object(m, 'TaskScope', FakeScope),patch.object(m,'gpu_usage',AsyncMock(return_value={'used_mib':0})):
                for _ in range(4):await node.reconcile()
            self.assertIsNotNone(node.isolation)
            node.store.db.close();node.instance_lock.close()

    async def test_missing_baseline_can_only_calibrate_after_three_low_idle_observations(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict(os.environ,AI_INFER_TOKEN='x'*48):node=m.Gateway(synthetic_config({'server':{'data_dir':raw}}))
            node.isolate('unknown',None,None)
            node.startup_checks = {'state': 'verified', 'test_double': True}
            with patch.object(m, 'verify_model_manifests', return_value={'synthetic': True}),patch.object(m, 'TaskScope', FakeScope),patch.object(m,'gpu_usage',AsyncMock(return_value={'used_mib':4000})):
                self.assertFalse(await node.calibrate_idle())
            self.assertIsNone(node.release_baseline)
            with patch.object(m, 'verify_model_manifests', return_value={'synthetic': True}),patch.object(m, 'TaskScope', FakeScope),patch.object(m,'gpu_usage',AsyncMock(return_value={'used_mib':43})),patch.object(m.asyncio,'sleep',AsyncMock()):
                self.assertTrue(await node.calibrate_idle())
                self.assertIsNotNone(node.isolation)
                for _ in range(3):await node.reconcile()
            self.assertIsNone(node.isolation)
            node.store.db.close();node.instance_lock.close()

    async def test_store_failure_has_terminal_receipt_and_degraded_state(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg={'server':{'data_dir':raw,'job_timeout_seconds':30,'reclaim_model_file_cache':False},'asr':{'python':sys.executable}}
            with patch.dict(os.environ,AI_INFER_TOKEN='x'*48):node=m.Gateway(synthetic_config(cfg))
            ident='a'*32;node.store.add(ident,ident,'asr',ident,{},'.wav');result=Path(raw)/ident/'result';result.mkdir(parents=True)
            (result/'metadata.json').write_text('{}');(result/'result.zip').write_bytes(b'fixture')
            update=node.store.update
            def broken(ident,**values):
                if values.get('status') in ('unloading','completed','failed'):raise OSError('injected disk failure')
                return update(ident,**values)
            with patch.object(node.store,'update',side_effect=broken),patch.object(m, 'verify_model_manifests', return_value={'synthetic': True}),patch.object(m, 'TaskScope', FakeScope),patch.object(m,'gpu_usage',AsyncMock(return_value={'used_mib':43})),patch.object(m.asyncio,'create_subprocess_exec',AsyncMock(return_value=Process())),patch.object(m,'kill_group'):
                await node.execute(ident)
            self.assertTrue((Path(raw)/ident/'terminal.json').exists());self.assertIsNotNone(node.scheduler_error)
            self.assertIsNone(node.current)
            node.store.db.close();node.instance_lock.close()
