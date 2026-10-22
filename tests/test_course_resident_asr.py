from tests import isolation

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import httpx

from qwen_asr_app.inference_providers import RemoteASRProvider, classroom_clip_operation
from qwen_asr_app.remote_classroom import ClassroomRemoteError, RemoteEmbedder, transcribe_clip
from qwen_asr_app.remote_operations import ResultNeedsRepair


class ClipRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / '.course-operations' / '00000-0-960000'
        self.folder.mkdir(parents=True)
        self.request = {'duration': 60, 'max_new_tokens': 728}

    def test_new_course_clip_reuses_resident_cuda_model(self):
        self.assertTrue(classroom_clip_operation(self.folder, self.request))

    def test_long_files_and_unsupported_token_budget_keep_durable_file_protocol(self):
        self.assertFalse(classroom_clip_operation(self.folder, {**self.request, 'duration': 120}))
        self.assertFalse(classroom_clip_operation(self.folder, {**self.request, 'max_new_tokens': 8193}))
        self.assertFalse(classroom_clip_operation(self.folder.parent.parent / 'upload', self.request))

    def test_saved_4096_budget_requires_upgraded_worker_and_is_not_reduced(self):
        request={**self.request,'max_new_tokens':4096,'config':{}}
        with patch('qwen_asr_app.remote_classroom.capabilities',return_value={'asr_session':True}):
            self.assertFalse(classroom_clip_operation(self.folder,request))
        with patch('qwen_asr_app.remote_classroom.capabilities',return_value={
                'asr_session':True,'classroom_asr_max_new_tokens':8192}):
            self.assertTrue(classroom_clip_operation(self.folder,request))

    def test_existing_file_receipt_never_starts_second_operation(self):
        # Every receipt state, including corrupt/uncertain data, pins recovery.
        for content in ('{"state":"prepared"}', '{"state":"submitted"}',
                        '{"state":"completed"}', 'corrupt'):
            (self.folder / 'receipt.json').write_text(content)
            self.assertFalse(classroom_clip_operation(self.folder, self.request))

    def test_cancel_intent_remains_on_original_protocol(self):
        (self.folder / 'receipt.cancel.json').write_text('{}')
        self.assertFalse(classroom_clip_operation(self.folder, self.request))

    def test_classroom_receipt_pins_recovery_and_conflict_requires_repair(self):
        (self.folder / 'classroom-receipt.json').write_text('{}')
        self.assertTrue(classroom_clip_operation(self.folder, self.request))
        (self.folder / 'receipt.json').write_text('{}')
        with self.assertRaises(ResultNeedsRepair):
            classroom_clip_operation(self.folder, self.request)

    def test_live_clip_keeps_resident_route(self):
        self.assertTrue(classroom_clip_operation(
            self.folder.parent.parent / 'live' / 'remote-operations' / 'clip', self.request))


class DurableClipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'audio.wav'
        self.source.write_bytes(b'unchanged audio fixture')

    def run_clip(self, **kwargs):
        return transcribe_clip(self.folder, {}, self.source, 'Chinese', 'terminology', 728, **kwargs)

    def test_busy_then_transport_uncertainty_reconciles_same_operation_id(self):
        seen = []
        def request(cfg, path, payload, **kwargs):
            seen.append(payload.copy())
            if len(seen) == 1:
                raise ClassroomRemoteError('WINDOWS_QUEUE_BUDGET_AUDIO_SAVED')
            if len(seen) == 2:
                raise httpx.ReadTimeout('response lost after inference')
            return {'text': '课堂内容', 'language': 'Chinese', 'seconds': 2.0,
                    'provider': 'remote', 'fallback': False}
        with patch('qwen_asr_app.remote_classroom.capabilities', return_value={'asr_session': True}), \
             patch('qwen_asr_app.remote_classroom.request', side_effect=request), \
             patch('qwen_asr_app.remote_classroom.time.sleep'):
            result = self.run_clip()
            # Saved result is reused without submitting a second inference.
            self.assertEqual(self.run_clip(), result)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(payload == seen[0] for payload in seen))
        saved = json.loads((self.folder / 'classroom-receipt.json').read_text())
        self.assertEqual(saved['operation_id'], seen[0]['operation_id'])
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(result['protocol'], 'classroom')
        self.assertGreaterEqual(result['client_seconds'], 0)
        self.assertNotIn('inference_seconds', result)  # Worker seconds includes cold loading.

    def test_permanent_worker_error_is_not_retried_or_locally_recomputed(self):
        with patch('qwen_asr_app.remote_classroom.capabilities', return_value={'asr_session': True}), \
             patch('qwen_asr_app.remote_classroom.request', side_effect=ClassroomRemoteError('ASR_TOKEN_LIMIT')) as send:
            with self.assertRaisesRegex(ClassroomRemoteError, 'ASR_TOKEN_LIMIT'):
                self.run_clip()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(json.loads((self.folder / 'classroom-receipt.json').read_text())['state'], 'submitting')

    def test_retry_exhaustion_retains_same_id_for_later_recovery(self):
        with patch('qwen_asr_app.remote_classroom.capabilities', return_value={'asr_session': True}), \
             patch('qwen_asr_app.remote_classroom.request', side_effect=ClassroomRemoteError('WINDOWS_BUSY_AUDIO_SAVED_RETRY')) as send, \
             patch('qwen_asr_app.remote_classroom.time.sleep'):
            with self.assertRaises(ResultNeedsRepair):
                self.run_clip()
        self.assertEqual(send.call_count, 12)
        saved = json.loads((self.folder / 'classroom-receipt.json').read_text())
        self.assertTrue(all(call.args[2]['operation_id'] == saved['operation_id'] for call in send.call_args_list))
        with patch('qwen_asr_app.remote_classroom.capabilities', return_value={'asr_session': True}), \
             patch('qwen_asr_app.remote_classroom.request', return_value={'text': 'recovered', 'seconds': .1}) as resumed:
            self.assertEqual(self.run_clip()['text'], 'recovered')
        self.assertEqual(resumed.call_args.args[2]['operation_id'], saved['operation_id'])

    def test_canceled_owner_does_not_submit_new_clip(self):
        stop = threading.Event(); stop.set()
        with patch('qwen_asr_app.remote_classroom.capabilities', return_value={'asr_session': True}), \
             patch('qwen_asr_app.remote_classroom.request') as send:
            with self.assertRaises(ResultNeedsRepair):
                self.run_clip(stop=stop)
        send.assert_not_called()

    def test_changed_source_cannot_reuse_a_durable_operation(self):
        with patch('qwen_asr_app.remote_classroom.capabilities', return_value={'asr_session': True}), \
             patch('qwen_asr_app.remote_classroom.request', return_value={'text': 'done', 'seconds': .1}) as send:
            self.run_clip()
            self.source.write_bytes(b'different audio')
            with self.assertRaisesRegex(ValueError, '版本发生变化'):
                self.run_clip()
        self.assertEqual(send.call_count, 1)

    def test_fastapi_detail_error_preserves_retryable_code(self):
        from qwen_asr_app.remote_classroom import request
        response = httpx.Response(409, json={'detail': 'WINDOWS_QUEUE_BUDGET_AUDIO_SAVED'})
        client = MagicMock()
        session = client.session.return_value.__enter__.return_value
        session.post.return_value = response
        with patch('qwen_asr_app.remote_classroom.remote_client', return_value=client):
            with self.assertRaises(ClassroomRemoteError) as found:
                request({}, '/v1/classroom/compute', {})
        self.assertEqual(found.exception.code, 'WINDOWS_QUEUE_BUDGET_AUDIO_SAVED')

    def test_old_resident_worker_cannot_silently_clamp_saved_generation_budget(self):
        with patch('qwen_asr_app.remote_classroom.capabilities',return_value={'asr_session':True}), \
             patch('qwen_asr_app.remote_classroom.request') as send:
            with self.assertRaisesRegex(ValueError,'生成上限'):
                transcribe_clip(self.folder,{},self.source,'Chinese','',4096)
        send.assert_not_called()

    def test_automatic_index_embedding_waits_for_shared_worker_without_duplicate_batch(self):
        embedder=RemoteEmbedder.__new__(RemoteEmbedder)
        embedder.config={'batch_size':2};embedder.routing={};embedder.contract={'dimension':1}
        seen=[]
        def request(cfg,path,payload,**kwargs):
            seen.append(payload.copy())
            if len(seen)==1:
                raise ClassroomRemoteError('WINDOWS_QUEUE_BUDGET_AUDIO_SAVED')
            return {'vectors':[[1.0] for _ in payload['texts']],
                    'contract':embedder.contract,'seconds':.2}
        with patch('qwen_asr_app.remote_classroom.request',side_effect=request), \
             patch('qwen_asr_app.remote_classroom.time.sleep'):
            self.assertEqual(embedder.encode_documents(['first','second','third']),[[1.0]]*3)
        self.assertEqual(seen[0],seen[1])
        self.assertNotEqual(seen[1]['operation_id'],seen[2]['operation_id'])
        self.assertEqual(seen[2]['texts'],['third'])
        self.assertEqual(embedder.last_seconds,.2)


class ProviderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_course_provider_uses_saved_config_and_resident_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / '.course-operations' / '00000-0-160'
            config = {'asr_provider': 'remote_only', 'base_url': 'http://example.invalid'}
            with patch('qwen_asr_app.remote_classroom.transcribe_clip', return_value={
                'text': 'actual clip result', 'language': 'Chinese', 'provider': 'remote',
                'protocol': 'classroom', 'seconds': .1,
            }) as resident, patch('qwen_asr_app.inference_providers.remote_client') as file_client:
                value, metadata = await RemoteASRProvider().transcribe_segments(
                    [0.0] * 160, 'Chinese', 'context', 384, config, operation_dir=folder)
            file_client.assert_not_called()
            self.assertEqual(resident.call_args.args[1], config)
            self.assertIsInstance(resident.call_args.kwargs['stop'], threading.Event)
            self.assertEqual(value['segments'][0]['end'], .01)
            self.assertEqual(metadata['protocol'], 'classroom')
            self.assertTrue((folder / 'request.json').is_file())


if __name__ == '__main__':
    unittest.main()
