"""Infrastructure tests use explicit fake workers, never count as model tests."""
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from fake_scope import FakeScope, config as synthetic_config
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
gateway_module = importlib.import_module("server.app")

FAKE = '''import json,os,sys,time,subprocess,zipfile
from pathlib import Path
r=json.loads(Path(sys.argv[2]).read_text());d=Path(r['job_dir'])
(d/'event.json').write_text(json.dumps({'state':'running','progress':10}))
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
(d/'child.pid').write_text(str(p.pid))
time.sleep(float(r['options'].get('prompt','0.4')))
o=d/'result';o.mkdir();(o/'metadata.json').write_text('{}')
(o/'transcript.json').write_text('{"text":"fixture"}')
with zipfile.ZipFile(o/'result.zip','w') as z:z.write(o/'transcript.json','transcript.json')
'''


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'workers').mkdir()
        (self.root / 'workers/__init__.py').touch()
        for kind in ('asr', 'mineru'):
            (self.root / f'workers/{kind}_worker.py').write_text(FAKE)
        import shutil
        shutil.copyfile(ROOT/'workers/scoped.py', self.root/'workers/scoped.py')
        self.token = 'x'*48
        self.cfg = {'server': {'data_dir': str(self.root/'data'), 'token_file': str(self.root/'token'),
            'job_timeout_seconds': 5, 'history_days': 7, 'max_upload_bytes': 1000,
            'upload_timeout_seconds': 3, 'max_queued_jobs': 32},
            'asr': {'python': sys.executable}, 'mineru': {'python': sys.executable}}
        synthetic_config(self.cfg)
        async def gpu():
            return {'used_mib': 43, 'total_mib': 8188}
        self.patches = [patch.object(gateway_module, 'ROOT', self.root),
            patch.object(gateway_module, 'TaskScope', FakeScope),
            patch.object(gateway_module, 'verify_model_manifests', return_value={'synthetic': True}),
            patch.object(gateway_module, 'configuration', return_value=self.cfg),
            patch.object(gateway_module, 'gpu_usage', gpu),
            patch.dict(os.environ, AI_INFER_TOKEN=self.token)]
        for item in self.patches:item.start()
        self.client = TestClient(gateway_module.app, client=('127.0.0.1', 50000))
        self.client.__enter__()
        self.headers = {'Authorization':'Bearer '+self.token,'Idempotency-Key':'test-request-123'}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def submit(self, key='test-request-123', kind='asr', delay='.4', body=b'input', filename='中文.wav'):
        return self.client.post('/v1/jobs', params={'kind':kind,'filename':filename,'options':json.dumps({'prompt':delay}) if kind=='asr' else '{}'},
            headers={**self.headers,'Idempotency-Key':key},content=body)

    def wait(self, ident, states):
        until=time.monotonic()+8
        while time.monotonic()<until:
            job=self.client.get('/v1/jobs/'+ident,headers=self.headers).json()['data']
            if job['status'] in states:return job
            time.sleep(.03)
        self.fail('Job did not reach '+str(states))

    def test_no_model_imports_auth_and_path_validation(self):
        self.assertEqual(self.client.get('/v1/health').status_code,401)
        self.assertTrue(self.client.get('/v1/health',headers=self.headers).json()['ok'])
        self.assertFalse(any(n.split('.')[0] in ('torch','transformers','vllm','mineru') for n in sys.modules))
        self.assertEqual(self.submit(filename='../secret.wav').status_code,400)
        self.assertEqual(self.submit(body=b'x'*1001).status_code,413)
        self.assertEqual(self.client.get('/v1/jobs/nope/result',headers=self.headers).status_code,404)

    def test_idempotency_and_conflict(self):
        first=self.submit().json()['data']['id']
        self.assertEqual(self.submit().json()['data']['id'],first)
        self.assertEqual(self.submit(body=b'different').status_code,409)

    def test_public_client_is_rejected_even_with_token(self):
        outside = TestClient(gateway_module.app, client=('8.8.8.8', 50000))
        response = outside.get('/v1/health', headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'NETWORK_NOT_ALLOWED')

    def test_serial_queue_and_cancel_cleans_descendants(self):
        first=self.submit(delay='20').json()['data']['id']
        self.wait(first,{'running'})
        second=self.submit(key='second-job-key',kind='mineru',filename='中文.pdf').json()['data']['id']
        self.assertEqual(self.client.get('/v1/jobs/'+second,headers=self.headers).json()['data']['status'],'queued')
        self.client.post('/v1/jobs/'+first+'/cancel',headers=self.headers)
        self.wait(first,{'canceled'})
        child=int((self.root/'data'/first/'child.pid').read_text())
        import psutil
        self.assertTrue(not psutil.pid_exists(child) or psutil.Process(child).status()=='zombie')
        self.assertEqual(self.wait(second,{'completed','failed'})['status'],'completed')
        self.assertEqual(self.client.get('/v1/jobs/'+second+'/result',headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/v1/jobs/'+second+'/result?format=../../secret',headers=self.headers).status_code,400)

    def test_timeout_releases_queue_and_restart_keeps_history(self):
        self.cfg['server']['job_timeout_seconds']=.2
        ident=self.submit(delay='20').json()['data']['id']
        value=self.wait(ident,{'failed'})
        self.assertEqual(value['error'],'JOB_TIMEOUT')
        self.client.__exit__(None,None,None)
        self.client=TestClient(gateway_module.app, client=('127.0.0.1', 50000))
        self.client.__enter__()
        self.assertEqual(self.client.get('/v1/jobs/'+ident,headers=self.headers).json()['data']['status'],'failed')

    def test_strong_kill_fake_worker_cleans_child_and_advances_queue(self):
        first=self.submit(delay='20').json()['data']['id']
        self.wait(first, {'running'})
        second=self.submit(key='after-strong-kill',kind='mineru',filename='fixture.pdf').json()['data']['id']
        node=self.client.get('/v1/status',headers=self.headers).json()['data']
        os.kill(node['worker_pid'],__import__('signal').SIGKILL)
        value=self.wait(first,{'failed'})
        self.assertEqual(value['error'],'WORKER_FAILED')
        self.assertTrue(value['metrics']['gpu_release_verified'], value['metrics'])
        child=int((self.root/'data'/first/'child.pid').read_text())
        import psutil
        self.assertTrue(not psutil.pid_exists(child) or psutil.Process(child).status()=='zombie')
        self.assertEqual(self.wait(second,{'completed','failed'})['status'],'completed')

    def test_dead_scheduler_is_never_healthy(self):
        # Cancel only this isolated scheduler task, never a production service.
        node=gateway_module.app.state.gateway
        self.client.portal.call(node.loop.cancel)
        self.assertEqual(self.client.get('/v1/health',headers=self.headers).status_code,503)
        # Replace it with a normal supervisor so teardown can shut down normally.
        async def restore():
            with __import__('contextlib').suppress(__import__('asyncio').CancelledError):
                await node.loop
            node.loop=__import__('asyncio').create_task(node.schedule())
        self.client.portal.call(restore)

    def test_separate_result_directory_and_private_bundle(self):
        node = gateway_module.app.state.gateway
        node.results = self.root / 'separate-results'
        node.results.mkdir(mode=0o700)
        ident = self.submit(key='separate-result-key').json()['data']['id']
        self.assertEqual(self.wait(ident, {'completed', 'failed'})['status'], 'completed')
        self.assertTrue((node.results / ident / 'result.zip').is_file())
        self.assertFalse((node.folder(ident) / 'result').exists())
        self.assertEqual((node.results / ident / 'result.zip').stat().st_mode & 0o777, 0o600)
        response = self.client.get('/v1/jobs/' + ident + '/result', headers=self.headers)
        self.assertEqual(response.status_code, 200)

    def test_startup_unverified_and_changed_source_block_new_jobs(self):
        node = gateway_module.app.state.gateway
        node.startup_checks = {'state': 'unverified'}
        self.assertEqual(self.client.get('/v1/health', headers=self.headers).status_code, 503)
        self.assertEqual(self.submit().status_code, 503)
        node.startup_checks = {'state': 'verified', 'test_double': True}
        with patch.object(gateway_module, 'source_identity', return_value={'changed': 'source'}):
            self.assertEqual(self.client.get('/v1/health', headers=self.headers).status_code, 503)
            self.assertEqual(self.submit().status_code, 503)


if __name__=='__main__':unittest.main()
