"""Synthetic install and idle checks; no models, cgroup or Windows deployment."""
import asyncio
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fake_scope import config as synthetic_config
from server.config import verify_model_manifests
from server.task_scope import TaskScope
from server.classroom import ClassroomCompute
gateway = importlib.import_module('server.app')


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provision = load_script('provision_models')
health = load_script('check_health')


class InstallerTests(unittest.TestCase):
    def fixture(self, root):
        shutil.copyfile(ROOT/'config.example.toml', root/'config.example.toml')
        cfg = provision.initialize(root)
        asr = Path(cfg['asr']['model_path'])
        asr.mkdir(parents=True)
        weight = asr/'model.safetensors'
        weight.write_bytes(b'synthetic ASR weight')
        provision.write_private(root/'models/asr-manifest.json', json.dumps({
            'source': 'huggingface', 'model': cfg['asr']['model_id'], 'revision': 'fixture',
            'files': {'model.safetensors': {'sha256': hashlib.sha256(weight.read_bytes()).hexdigest()}}}))
        mineru = Path(cfg['mineru']['home'])/'models'
        mineru.mkdir(parents=True)
        (mineru/'model.safetensors').write_bytes(b'synthetic MinerU weight')
        return cfg

    def test_initialize_preserves_existing_settings_and_provisions_private_token(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.fixture(root)
            token = (root/'private/token').read_bytes()
            path = root/'config.toml'
            text = path.read_text().replace('port = 8765', 'port = 9876') + '\n[knowledge]\noperator_setting = "keep this"\n'
            path.write_text(text)
            cfg = provision.initialize(root)
            self.assertEqual(cfg['server']['port'], 9876)
            self.assertEqual(cfg['knowledge']['operator_setting'], 'keep this')
            self.assertEqual(cfg['server']['worker_cgroup_root'], 'auto')
            self.assertEqual((root/'private/token').read_bytes(), token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root/'private/token').stat().st_mode & 0o777, 0o600)

    def test_install_provisions_both_manifests_that_gateway_really_verifies(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cfg = self.fixture(root)
            with patch.object(provision.subprocess, 'run') as official_verify:
                hashes = provision.provision(root)
            self.assertEqual(official_verify.call_args.args[0][1:3], ['models', 'verify'])
            self.assertEqual(official_verify.call_args.kwargs['env']['MINERU_HOME'], cfg['mineru']['home'])
            installed = tomllib.loads((root/'config.toml').read_text())
            self.assertEqual(verify_model_manifests(installed), hashes)
            Path(installed['mineru']['home'], 'models/model.safetensors').write_bytes(b'tampered bytes')
            with self.assertRaisesRegex(ValueError, 'MODEL_CONTENT_MISMATCH'):
                verify_model_manifests(installed)

    def test_failed_official_verifier_cannot_produce_a_trusted_pin(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.fixture(root)
            original = (root/'config.toml').read_bytes()
            with patch.object(provision.subprocess, 'run', side_effect=RuntimeError('official verify failed')):
                with self.assertRaises(RuntimeError):
                    provision.provision(root)
            self.assertEqual((root/'config.toml').read_bytes(), original)
            self.assertFalse((root/'models/mineru-manifest.json').exists())

    def test_bad_asr_hash_does_not_publish_candidate_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cfg = self.fixture(root)
            original = (root/'config.toml').read_bytes()
            Path(cfg['asr']['model_path'], 'model.safetensors').write_bytes(b'unverified ASR')
            with patch.object(provision.subprocess, 'run'):
                with self.assertRaisesRegex(ValueError, 'MODEL_CONTENT_MISMATCH'):
                    provision.provision(root)
            self.assertEqual((root/'config.toml').read_bytes(), original)

    def test_health_check_rejects_http_success_with_unverified_startup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.fixture(root)
            healthy = {'ok': True, 'data': {'status': 'healthy', 'startup_checks': {'state': 'verified'}}}
            opener = SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(json.dumps(healthy).encode()))
            with patch.object(health.urllib.request, 'build_opener', return_value=opener):
                health.wait_healthy(root/'config.toml', .05)
                healthy['data']['startup_checks']['state'] = 'unverified'
                with self.assertRaisesRegex(RuntimeError, 'readiness failed'):
                    health.wait_healthy(root/'config.toml', .02)

    def test_auto_scope_still_requires_owned_service_and_cgroup_kill(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = root/'sys/system.slice/ai-infer-gateway.service'
            service.mkdir(parents=True)
            (service/'cgroup.controllers').touch()
            proc = root/'proc-cgroup'
            proc.write_text('0::/system.slice/ai-infer-gateway.service\n')
            def mapped(value):
                return {'/proc/self/cgroup': proc, '/sys/fs/cgroup': root/'sys'}.get(value, Path(value))
            original_mkdir = Path.mkdir
            def scope_dir(path, *args, **kwargs):
                original_mkdir(path, *args, **kwargs)
                if path.name.startswith('ai-job-'):
                    (path/'cgroup.kill').touch()
                    (path/'cgroup.events').write_text('populated 0\n')
                    (path/'cgroup.procs').touch()
            with patch('server.task_scope.Path', side_effect=mapped), \
                 patch.object(Path, 'mkdir', autospec=True, side_effect=scope_dir):
                scope = TaskScope('auto', 'a'*32)
                self.assertEqual(scope.path.parent, service)
                self.assertFalse(scope.observation()['populated'])
                with self.assertRaisesRegex(RuntimeError, 'NOT_OWNED_BY_SERVICE'):
                    TaskScope(root, 'b'*32)
                with patch('server.task_scope.os.getuid', return_value=os.getuid()+1):
                    with self.assertRaisesRegex(RuntimeError, 'NOT_OWNED_BY_SERVICE'):
                        TaskScope('auto', 'c'*32)

    def test_systemd_installer_requires_delegation_and_authenticated_health(self):
        unit = (ROOT/'scripts/install_systemd.sh').read_text()
        self.assertIn('Delegate=yes', unit)
        self.assertIn('scripts/check_health.py', unit)
        self.assertIn('KillMode=control-group', unit)
        self.assertIn('NoNewPrivileges=true', unit)


class IdleGatewayTests(unittest.IsolatedAsyncioTestCase):
    def node(self, root):
        cfg = synthetic_config({'server': {'data_dir': str(root), 'history_days': 7},
            'asr': {}, 'mineru': {}})
        with patch.dict(os.environ, AI_INFER_TOKEN='x'*48):
            return gateway.Gateway(cfg)

    async def test_idle_queue_waits_then_dispatches_immediately_on_event(self):
        with tempfile.TemporaryDirectory() as raw:
            node = self.node(Path(raw))
            available = [None]
            ran = asyncio.Event()
            async def execute(ident):
                self.assertEqual(ident, 'fixture')
                available[0] = None
                ran.set()
            node.execute = execute
            with patch.object(node.store, 'queued', side_effect=lambda: available[0]) as queued:
                node.loop = asyncio.create_task(node.schedule())
                try:
                    await asyncio.sleep(.6)
                    self.assertEqual(queued.call_count, 1)
                    available[0] = 'fixture'
                    node.work_available.set()
                    await asyncio.wait_for(ran.wait(), .3)
                finally:
                    await asyncio.wait_for(node.stop(), .3)

    async def test_status_probes_are_shared_without_caching_verification_functions(self):
        with tempfile.TemporaryDirectory() as raw:
            node = self.node(Path(raw))
            observe = AsyncMock(return_value={'state': 'observed', 'used_mib': 42})
            try:
                values = await asyncio.gather(*(node.status_observation('host', observe, 15) for _ in range(20)))
                observe.assert_awaited_once()
                self.assertTrue(all(value['sample_interval_seconds'] == 15 for value in values))
                # Fresh ownership checks bypass the dashboard wrapper entirely.
                self.assertEqual((await observe())['used_mib'], 42)
                self.assertEqual(observe.await_count, 2)
            finally:
                await node.stop()

    async def test_classroom_reaper_exits_when_no_worker_remains(self):
        with tempfile.TemporaryDirectory() as raw:
            service = ClassroomCompute(SimpleNamespace(data=Path(raw), cfg={'server': {}}))
            with patch('server.classroom.asyncio.sleep', new_callable=AsyncMock) as sleep:
                timer = asyncio.create_task(service.reap())
                service.timer = timer
                await timer
            sleep.assert_not_awaited()
            self.assertIsNone(service.timer)


if __name__ == '__main__':
    unittest.main()
