import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client.remote import RemoteClient, RemoteError, RemotePending


class RemoteClientTests(unittest.TestCase):
    def test_document_progress_keeps_detail_and_unknown_total_through_download(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('markdown.md', 'fixture')
        states = iter([{'status': 'running', 'progress': None, 'detail': 'MinerU · OCR 12/24'},
                       {'status': 'completed', 'progress': 100}])
        def handle(request):
            path = request.url.path
            if path == '/v1/health': value = {'status':'healthy','models_preloaded':False}
            elif path == '/v1/jobs': value = {'id': 'e'*32}
            elif path.endswith('/result'): return httpx.Response(200,content=archive.getvalue())
            else: value = next(states)
            return httpx.Response(200,json={'ok':True,'data':value})
        with tempfile.TemporaryDirectory() as raw, patch('client.remote.time.sleep'):
            source = Path(raw)/'fixture.pdf'; source.write_bytes(b'fixture')
            updates = []
            self.client(handle).run('mineru', source, Path(raw)/'out', progress=lambda *v: updates.append(v))
            self.assertIn(('Windows · MinerU · OCR 12/24', None), updates)
            self.assertTrue(all(value is None for _,value in updates))
            self.assertIn('下载', updates[-1][0])

    def client(self, handler):
        with patch.dict(os.environ, AI_INFER_TOKEN='x'*48):
            client = RemoteClient({'base_url':'http://100.81.62.69:8765', 'token_file':'unused',
                'connect_timeout_seconds':1, 'request_timeout_seconds':30,
                'job_timeout_seconds':120, 'poll_seconds':.01, 'network_recovery_seconds':1})
        transport = httpx.MockTransport(handler)
        client.session = lambda: httpx.Client(base_url=client.base_url, transport=transport)
        return client

    def test_lost_submit_response_reuses_key_body_and_job_while_poll_recovers(self):
        state = {'health':0,'posts':[],'polls':0}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive,'w') as bundle:
            bundle.writestr('transcript.txt','中文 English')
        def handle(request):
            path = request.url.path
            if path == '/v1/health':
                state['health'] += 1
                if state['health'] == 1:raise httpx.ConnectError('temporary',request=request)
                return httpx.Response(200,json={'ok':True,'data':{'status':'healthy','models_preloaded':False}})
            if path == '/v1/jobs':
                state['posts'].append((request.headers['idempotency-key'],request.content))
                if len(state['posts']) == 1:raise httpx.ReadError('response lost after acceptance',request=request)
                return httpx.Response(202,json={'ok':True,'data':{'id':'a'*32,'status':'queued'}})
            if path.endswith('/result'):
                return httpx.Response(200,content=archive.getvalue())
            if path.startswith('/v1/operations/'):
                return httpx.Response(200,json={'ok':True,'data':{'id':'a'*32}})
            state['polls'] += 1
            if state['polls'] == 1:raise httpx.ConnectError('temporary polling failure',request=request)
            if state['polls'] == 2:return httpx.Response(503,json={'ok':False,'error':'busy'})
            return httpx.Response(200,json={'ok':True,'data':{'status':'completed','progress':100,'metrics':{}}})
        with tempfile.TemporaryDirectory() as raw, patch('client.remote.time.sleep'):
            source=Path(raw)/'中文.wav';source.write_bytes(b'original audio')
            with self.assertRaises(RemotePending):
                self.client(handle).run('asr',source,Path(raw)/'out')
            result=self.client(handle).run('asr',source,Path(raw)/'out')
            self.assertEqual(result['job_id'],'a'*32)
            self.assertEqual(state['posts'][0][1], b'original audio')
            self.assertEqual(len(state['posts']),1)
            self.assertEqual(source.read_bytes(),b'original audio')
            self.assertEqual((Path(raw)/'out/transcript.txt').read_text(),'中文 English')

    def test_authentication_failure_is_not_retried(self):
        calls=[]
        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(401,json={'ok':False,'error':'UNAUTHORIZED'})
        with self.assertRaisesRegex(RemoteError,'REMOTE_HTTP_401'):
            self.client(handle).health()
        self.assertEqual(calls,['/v1/health'])

    def test_persistent_disconnect_preserves_accepted_job_for_recovery(self):
        requests=[]
        def handle(request):
            requests.append((request.method,request.url.path))
            if request.url.path=='/v1/health':return httpx.Response(200,json={'ok':True,'data':{'status':'healthy','models_preloaded':False}})
            if request.url.path=='/v1/jobs':return httpx.Response(202,json={'ok':True,'data':{'id':'b'*32}})
            if request.method=='POST':return httpx.Response(200,json={'ok':True})
            raise httpx.ConnectError('disconnected',request=request)
        client=self.client(handle);client.cfg['network_recovery_seconds']=.01
        with tempfile.TemporaryDirectory() as raw:
            source=Path(raw)/'audio.wav';source.write_bytes(b'audio')
            with self.assertRaisesRegex(RemoteError,'REQUIRES_RECOVERY'):
                client.run('asr',source,Path(raw)/'out')
        self.assertEqual(requests.count(('POST','/v1/jobs')),1)
        self.assertNotIn(('POST','/v1/jobs/'+'b'*32+'/cancel'), requests)


if __name__=='__main__':unittest.main()

class DurableRemoteRepairTests(unittest.TestCase):
    client = RemoteClientTests.client
    def test_three_lost_responses_then_new_client_reconciles_one_server_job(self):
        state={'keys':[], 'jobs':{}, 'lookups':0}
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:z.writestr('transcript.txt','ground truth')
        def handle(request):
            path=request.url.path
            if path=='/v1/health':return httpx.Response(200,json={'ok':True,'data':{'status':'healthy','models_preloaded':False}})
            if path=='/v1/jobs':
                key=request.headers['idempotency-key'];state['keys'].append(key);state['jobs'].setdefault(key,'c'*32)
                raise httpx.ReadError('accepted response lost',request=request)
            if path.startswith('/v1/operations/'):
                state['lookups']+=1
                return httpx.Response(200,json={'ok':True,'data':{'id':state['jobs'][path.split('/')[-1]]}})
            if path.endswith('/result'):return httpx.Response(200,content=archive.getvalue())
            return httpx.Response(200,json={'ok':True,'data':{'status':'completed','progress':100,'metrics':{}}})
        with tempfile.TemporaryDirectory() as raw,patch('client.remote.time.sleep'):
            source=Path(raw)/'audio.wav';source.write_bytes(b'synthetic')
            with self.assertRaises(RemotePending):self.client(handle).run('asr',source,Path(raw)/'out')
            result=self.client(handle).run('asr',source,Path(raw)/'out')
            self.assertEqual(result['job_id'],'c'*32)
            self.assertEqual(len(state['keys']),1);self.assertEqual(len(state['jobs']),1);self.assertEqual(state['lookups'],1)
            self.assertEqual((Path(raw)/'out/transcript.txt').read_text(),'ground truth')

    def test_completed_download_interruption_retries_same_id_without_cancel(self):
        events=[];archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:z.writestr('transcript.txt','complete')
        def handle(request):
            path=request.url.path;events.append((request.method,path))
            if path=='/v1/health':return httpx.Response(200,json={'ok':True,'data':{'status':'healthy','models_preloaded':False}})
            if path=='/v1/jobs':return httpx.Response(202,json={'ok':True,'data':{'id':'d'*32}})
            if path.endswith('/result'):
                if sum(p.endswith('/result') for _,p in events)==1:raise httpx.ReadError('download interrupted',request=request)
                return httpx.Response(200,content=archive.getvalue())
            return httpx.Response(200,json={'ok':True,'data':{'status':'completed','progress':100,'metrics':{}}})
        with tempfile.TemporaryDirectory() as raw,patch('client.remote.time.sleep'):
            source=Path(raw)/'audio.wav';source.write_bytes(b'audio');out=Path(raw)/'out'
            self.client(handle).run('asr',source,out)
            self.assertFalse((out/'result.zip.part').exists());self.assertEqual((out/'transcript.txt').read_text(),'complete')
        self.assertEqual(sum(p.endswith('/result') for _,p in events),2);self.assertFalse(any(p.endswith('/cancel') for _,p in events))
