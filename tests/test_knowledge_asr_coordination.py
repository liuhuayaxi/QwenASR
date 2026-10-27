from tests import isolation  # noqa: F401

import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qwen_asr_app.knowledge.embeddings import EmbeddingWorkerExited, LocalEmbedder
from qwen_asr_app.knowledge.jobs import JobManager
from qwen_asr_app.knowledge.service import KnowledgeService
from qwen_asr_app.knowledge.store import KnowledgeStore


async def until(predicate):
    for _ in range(250):
        if predicate():
            return
        await asyncio.sleep(.01)
    raise AssertionError('condition not reached')


class DeferredIndexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp.name) / 'knowledge.db')
        self.busy = True
        self.sync = Mock(return_value={'sources_checked': 1})
        self.manager = JobManager(self.store, self.sync, defer=lambda _: self.busy)
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    async def test_busy_asr_coalesces_index_requests_then_runs_once_after_completion(self):
        ident = self.manager.schedule('物理')
        await until(lambda: self.store.get_job(ident)['stage'] == 'waiting_asr')
        self.assertEqual(self.manager.schedule('物理'), ident)
        self.sync.assert_not_called()
        self.assertEqual(self.store.get_job(ident)['status'], 'queued')
        self.busy = False
        await until(lambda: self.store.get_job(ident)['status'] == 'completed')
        self.sync.assert_called_once()
        self.assertFalse(self.store.get_job(ident).get('error'))

    async def test_cancel_while_waiting_does_not_start_embedding(self):
        ident = self.manager.schedule('物理')
        await until(lambda: self.store.get_job(ident)['stage'] == 'waiting_asr')
        self.assertTrue(self.manager.cancel(ident))
        await self.manager._queue.join()
        self.assertEqual(self.store.get_job(ident)['status'], 'cancelled')
        self.sync.assert_not_called()

    async def test_asr_starting_during_indexing_requeues_same_job_without_failure(self):
        self.busy = False
        def sync(**kwargs):
            if self.sync.call_count == 1:
                self.busy = True
                raise RuntimeError('Embedding failure: 本机模型资源繁忙，任务进度已保留')
            return {'sources_checked': 1}
        self.sync.side_effect = sync
        ident = self.manager.schedule('物理')
        await until(lambda: self.store.get_job(ident)['stage'] == 'waiting_asr')
        self.assertEqual(self.store.get_job(ident)['status'], 'queued')
        self.assertEqual(self.manager.schedule('物理'), ident)
        self.busy = False
        await until(lambda: self.store.get_job(ident)['status'] == 'completed')
        self.assertEqual(self.sync.call_count, 2)
        self.assertFalse(self.store.get_job(ident).get('error'))

    async def test_shutdown_preserves_waiting_job_for_recovery(self):
        ident = self.manager.schedule('物理')
        await until(lambda: self.store.get_job(ident)['stage'] == 'waiting_asr')
        await asyncio.wait_for(self.manager.close(), 2)
        self.assertEqual(self.store.get_job(ident)['status'], 'interrupted')
        self.sync.assert_not_called()

    def test_local_background_index_waits_for_any_active_recording(self):
        service = KnowledgeService.__new__(KnowledgeService)
        record = SimpleNamespace(metadata={'category': '光电子学', 'status': 'processing'})
        service.recording_store = SimpleNamespace(list_records=lambda: [record])
        with patch('qwen_asr_app.inference_providers.inference_settings.for_task',
                   return_value={'knowledge_provider': 'local'}):
            self.assertTrue(service._defer_indexing('物理'))
            record.metadata['status'] = 'completed'
            self.assertFalse(service._defer_indexing('物理'))

    def test_live_capture_is_detected_without_loading_full_transcripts(self):
        service = KnowledgeService.__new__(KnowledgeService)
        service.recording_store = SimpleNamespace(
            iter_list_items=lambda: iter([{'status': 'active'}]),
            list_records=Mock(side_effect=AssertionError('must not load transcript bodies')))
        with patch('qwen_asr_app.inference_providers.inference_settings.for_task',
                   return_value={'knowledge_provider': 'local'}):
            self.assertTrue(service._defer_indexing('物理'))
        service.recording_store.list_records.assert_not_called()
        with patch('qwen_asr_app.inference_providers.inference_settings.for_task',
                   return_value={'knowledge_provider': 'remote_only'}):
            self.assertFalse(service._defer_indexing('物理'))


class EmbeddingWorkerRecoveryTests(unittest.TestCase):
    def encode_with_replies(self, failures):
        embedder = LocalEmbedder()
        writes = []
        embedder.process = SimpleNamespace(stdin=SimpleNamespace(
            write=lambda value: writes.append(json.loads(value)), flush=lambda: None))
        replies = iter(failures)
        def read(_timeout):
            failure = next(replies)
            if failure:
                raise failure
            return {'type': 'result', 'id': writes[-1]['id'], 'contract': embedder.contract,
                    'vectors': [[1.] + [0.] * (embedder.contract['dimension'] - 1)],
                    'seconds': .1, 'memory': {}}
        with patch('qwen_asr_app.knowledge.embeddings.metal_lease', return_value=nullcontext()), \
             patch.object(embedder, '_start') as start, \
             patch.object(embedder, 'close') as close, \
             patch.object(embedder, '_read', side_effect=read):
            try:
                value = embedder.encode_documents(['完整的知识块'])
                return value, writes, start.call_count, close.call_count
            finally:
                embedder.process = None

    def test_worker_idle_exit_between_poll_and_reply_retries_identical_batch_once(self):
        value, writes, starts, closes = self.encode_with_replies([EmbeddingWorkerExited('idle exit'), None])
        self.assertEqual(len(value), 1)
        self.assertEqual(writes[0], writes[1])
        self.assertEqual((starts, closes), (2, 1))

    def test_repeated_worker_exit_is_reported_without_an_infinite_restart_loop(self):
        with self.assertRaises(EmbeddingWorkerExited):
            self.encode_with_replies([EmbeddingWorkerExited('first exit'), EmbeddingWorkerExited('second exit')])

    def test_generation_timeout_is_not_retried_as_an_idle_exit(self):
        with self.assertRaises(TimeoutError):
            self.encode_with_replies([TimeoutError('generation timeout')])


if __name__ == '__main__':
    unittest.main()
