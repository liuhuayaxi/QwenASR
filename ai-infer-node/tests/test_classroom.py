import asyncio
import fcntl
import importlib.util
import sqlite3
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server.classroom import ClassroomCompute
from workers.classroom_worker import asr_token_limit

spec=importlib.util.spec_from_file_location('classroom_deploy',Path(__file__).resolve().parents[2]/'scripts/deploy_classroom_extension.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)


class ClassroomDeploymentTests(unittest.TestCase):
    def test_resident_token_budget_preserves_saved_parameters_and_rejects_invalid_values(self):
        for limit in (128,512,2048,4096,8192):
            self.assertEqual(asr_token_limit(limit),limit)
        for limit in (True,4096.0,'4096',127,8193,None):
            with self.assertRaisesRegex(ValueError,'INVALID_ASR_TOKEN_LIMIT'):
                asr_token_limit(limit)

    def test_protocol_one_upgrade_is_additive_backed_up_and_repeatable(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'data').mkdir();(root/'server').mkdir();(root/'workers').mkdir();(root/'config.toml').write_text('preserved=true')
            with sqlite3.connect(root/'data/jobs.sqlite3') as db:db.execute('create table jobs(status text)')
            source='''class Gateway:
    async def schedule(self):
        while not self.stopping:
            pass
    async def execute(self, ident):
        pass
    async def stop(self):
        self.stopping = True
def validated_options(tokens):
    if not 128 <= tokens <= 2048:
        raise ValueError()
app = FastAPI(title="legacy")
'''
            (root/'server/app.py').write_text(source)
            incoming=root/'incoming';(incoming/'server').mkdir(parents=True);(incoming/'workers').mkdir()
            for name in ('server/classroom.py','workers/classroom_worker.py'):(incoming/name).write_text('IMPLEMENTATION = 1\n')
            first=deploy.install(root,incoming)
            text=(root/'server/app.py').read_text();self.assertIn('tokens <= 8192',text)
            self.assertEqual(text.count('install_classroom(app)'),1)
            backups=list((root/'backups').glob('classroom-*'));self.assertEqual(len(backups),1)
            self.assertEqual((backups[0]/'server/app.py').read_text(),source)
            with sqlite3.connect(backups[0]/'jobs.sqlite3') as db:self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual((root/'config.toml').read_text(),'preserved=true')

    def test_gateway_lifetime_lock_blocks_all_source_mutations(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'data').mkdir()
            with (root/'data/gateway.lock').open('a') as held:
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError,'gracefully'):deploy.install(root,root/'incoming')
            self.assertFalse((root/'backups').exists())

    def test_queued_work_prevents_install_after_service_stops(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'data').mkdir()
            with sqlite3.connect(root/'data/jobs.sqlite3') as db:
                db.execute('create table jobs(status text)');db.execute("insert into jobs values ('queued')")
            with self.assertRaisesRegex(RuntimeError,'active tasks'):deploy.install(root,root/'incoming')
            self.assertFalse((root/'backups').exists())


class ClassroomResourceTests(unittest.IsolatedAsyncioTestCase):
    def service(self, root):
        return ClassroomCompute(SimpleNamespace(data=root,cfg={'server':{},'asr':{'python':'unused-python','model_path':'unused-model'}},current=None))

    async def test_failed_worker_spawn_releases_real_gpu_file_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);service=self.service(root)
            with patch('server.classroom.asyncio.create_subprocess_exec',side_effect=OSError('cannot spawn')):
                with self.assertRaises(OSError):await service.run({'kind':'asr','operation_id':'spawn-failure-123','audio':'fixture'})
            with (root/'gpu.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIsNone(service.process);self.assertIsNone(service.gpu_lock);self.assertIsNone(service.log)

    async def test_cpu_embedding_does_not_claim_gpu_and_asr_acquires_before_reusing_worker(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);service=self.service(root);resident=SimpleNamespace(returncode=None)
            service.process=resident
            await service.start(needs_gpu=False)
            self.assertIsNone(service.gpu_lock)
            await service.start(needs_gpu=True)
            self.assertIs(service.process,resident)
            with (root/'gpu.lock').open('a') as probe:
                with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
            service.process=None;await service.close()

    async def test_classroom_reservations_expire_and_resource_isolation_blocks_load(self):
        with tempfile.TemporaryDirectory() as raw:
            service=self.service(Path(raw));service.sessions['lesson']=100
            with patch('server.classroom.time.monotonic',return_value=99):self.assertTrue(service.reserved())
            with patch('server.classroom.time.monotonic',return_value=101):self.assertFalse(service.reserved())
            service.node.isolation={'reason':'unverified GPU ownership'}
            with self.assertRaisesRegex(Exception,'NODE_RESOURCE_ISOLATION'):await service.start()
            self.assertIsNone(service.process)


if __name__=='__main__':unittest.main()
