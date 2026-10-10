"""Safe expectations for the independent review's transport fault matrix."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import sys
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client.remote import RemoteClient, RemotePending, ProvenNotAccepted, atomic_receipt, request_cancel

CFG = {'base_url': 'http://127.0.0.1:8765', 'token_file': '/missing/synthetic-token',
    'mineru_provider': 'remote_preferred', 'connect_timeout_seconds': 1, 'request_timeout_seconds': 30,
    'job_timeout_seconds': 60, 'poll_seconds': .001}


class AuditTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'synthetic.pdf'
        self.source.write_bytes(b'synthetic-input')
        self.path = self.root/'private/receipt.json'
        self.out = self.root/'out'
        self.requests = []
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('markdown.md', 'synthetic result')
        self.archive = archive.getvalue()

    def client(self, handler=None):
        def handle(r):
            self.requests.append((r.method, r.url.path))
            if handler:
                result = handler(r)
                if result is not None:
                    return result
            if r.url.path == '/v1/health':
                return httpx.Response(200, json={'ok': True, 'data': {'status': 'healthy', 'models_preloaded': False}})
            if r.url.path.endswith('/result'):
                return httpx.Response(200, content=self.archive)
            return httpx.Response(200, json={'ok': True, 'data': {'id': 'job-1', 'status': 'completed', 'progress': 100}})
        client = RemoteClient(CFG)
        client.session = lambda: httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handle))
        return client

    def run_client(self, client):
        return client.run('mineru', self.source, self.out, receipt_path=self.path)

    def prepare(self, state, job=None):
        client = self.client()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        receipt = client._read_or_prepare('mineru', self.source, {}, 'fixed-operation-key', self.path)
        receipt.update(state=state, job_id=job)
        atomic_receipt(self.path, receipt)

    def test_fresh_health_failure_is_proven_not_accepted(self):
        def offline(r): raise httpx.ConnectError('offline')
        with self.assertRaises(ProvenNotAccepted), patch('client.remote.time.sleep'):
            self.run_client(self.client(offline))
        self.assertFalse(any(m == 'POST' for m, _ in self.requests))

    def test_pending_health_failure_does_not_recompute_or_query_health(self):
        self.prepare('submitting')
        def denied(r): return httpx.Response(401)
        with self.assertRaises(RemotePending): self.run_client(self.client(denied))
        self.assertEqual(self.requests, [('GET', '/v1/operations/fixed-operation-key')])

    def test_corrupt_receipt_is_preserved(self):
        self.path.parent.mkdir(); self.path.write_text('{bad')
        with self.assertRaises(RemotePending): self.run_client(self.client())
        self.assertEqual(self.path.read_text(), '{bad')
        self.assertEqual(self.requests, [])

    def test_completed_missing_input_goes_directly_to_download(self):
        self.prepare('completed', 'job-1'); self.source.unlink()
        result = self.run_client(self.client())
        self.assertEqual(result['job_id'], 'job-1')
        self.assertEqual(self.requests, [('GET', '/v1/jobs/job-1/result')])

    def test_pending_missing_token_is_pending(self):
        self.prepare('submitting')
        with patch.dict('os.environ', {'AI_INFER_TOKEN': ''}), self.assertRaises(RemotePending):
            self.run_client(RemoteClient(CFG))

    def test_accepted_bad_json_is_pending_with_durable_key(self):
        def bad(r):
            if r.method == 'POST':
                self.assertEqual(json.loads(self.path.read_text())['state'], 'submitting')
                return httpx.Response(202, content=b'not json')
        with self.assertRaises(RemotePending): self.run_client(self.client(bad))
        self.assertTrue(json.loads(self.path.read_text())['key'])
        self.assertEqual(sum(m == 'POST' for m, _ in self.requests), 1)

    def test_lost_response_then_401_never_posts_again(self):
        def lost(r):
            if r.method == 'POST': raise httpx.ReadError('accepted response lost')
        with self.assertRaises(RemotePending): self.run_client(self.client(lost))
        key = json.loads(self.path.read_text())['key']
        with self.assertRaises(RemotePending): self.run_client(self.client(lambda r: httpx.Response(401)))
        self.assertEqual(sum(m == 'POST' for m, _ in self.requests), 1)
        self.assertEqual(json.loads(self.path.read_text())['key'], key)

    def test_cancel_unknown_and_404_never_resubmits(self):
        self.prepare('cancel_unconfirmed')
        with self.assertRaises(RemotePending): self.run_client(self.client(lambda r: httpx.Response(404)))
        self.assertFalse(any(m == 'POST' for m, _ in self.requests))
        self.assertTrue(json.loads(self.path.read_text())['cancel_requested'])

    def test_idle_cancel_intent_prevents_submit(self):
        self.prepare('prepared'); request_cancel(self.path)
        with self.assertRaises(RemotePending): self.run_client(self.client())
        self.assertEqual(self.requests, [])

    def test_download_recovery_and_offline_materialize_need_no_input_or_credentials(self):
        self.prepare('completed', 'job-1')
        self.run_client(self.client())
        self.source.unlink(); self.requests.clear()
        result = self.run_client(RemoteClient(CFG))
        self.assertEqual(result['state'], 'downloaded')
        self.assertEqual(self.requests, [])

    def test_failed_accepted_operation_never_becomes_fallback(self):
        self.prepare('failed', 'job-1')
        with self.assertRaises(RemotePending): self.run_client(self.client())
        self.assertEqual(self.requests, [])

    def test_changed_settings_do_not_change_saved_endpoint_and_options(self):
        self.prepare('submitting')
        c = self.client(); c.cfg['base_url'] = 'bad-url'
        self.run_client(c)
        self.assertEqual(c.base_url, CFG['base_url'])
        self.assertFalse(any(m == 'POST' for m, _ in self.requests))

    def test_receipt_fsync_failure_forbids_post(self):
        with patch('client.remote.os.fsync', side_effect=OSError('disk failed')), self.assertRaises(OSError):
            self.run_client(self.client())
        self.assertEqual(self.requests, [])

    def test_missing_or_changed_immutable_input_never_becomes_fallback(self):
        self.prepare('prepared')
        blob = Path(json.loads(self.path.read_text())['blob_path'])
        blob.write_bytes(b'changed')
        with self.assertRaises(RemotePending):
            self.run_client(self.client())
        self.assertEqual(json.loads(self.path.read_text())['state'], 'prepared')
        blob.unlink()
        with self.assertRaises(RemotePending):
            self.run_client(self.client())
        self.assertFalse(any(method == 'POST' for method, _ in self.requests))

    def test_result_materialization_is_private_atomic_and_preserves_existing_result(self):
        self.prepare('completed', 'job-1')
        self.run_client(self.client())
        self.assertEqual((self.out/'markdown.md').stat().st_mode & 0o777, 0o600)
        (self.out/'markdown.md').write_text('existing local changes')
        self.requests.clear()
        with self.assertRaises(RemotePending):
            self.run_client(self.client())
        self.assertEqual((self.out/'markdown.md').read_text(), 'existing local changes')
        self.assertEqual(self.requests, [])

    def test_recursive_case_insensitive_secret_filter(self):
        from client.remote import without_secrets
        value = without_secrets({'Authorization':'hidden', 'token_file':'reference',
            'nested':{'PASSWORD':'hidden','ssh_private_key':'hidden','api-key':'hidden'}})
        self.assertEqual(value, {'token_file':'reference', 'nested':{}})

if __name__ == '__main__': unittest.main()
