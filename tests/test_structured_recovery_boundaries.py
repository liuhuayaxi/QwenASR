from tests import isolation  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from qwen_asr_app.ai import AIRequestError, is_recoverable_structured_error
from qwen_asr_app.batch_ai import correct_batch_segments
from qwen_asr_app.storage import RecordingRecord


class StructuredRecoveryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _record(self, directory: str) -> RecordingRecord:
        record = RecordingRecord(Path(directory), {
            'id': 'structured-boundary',
            'title': '测试课堂',
            'category': '测试课程',
            'status': 'processing',
            'sample_count': 0,
        })
        for ident in (1, 2):
            record.upsert_segment({
                'id': ident,
                'raw': f'原文{ident}',
                'start': ident - 1,
                'end': ident,
                'language': 'Chinese',
                'ai_pending': True,
            })
        return record

    def test_only_structured_output_failures_are_recoverable(self):
        self.assertTrue(is_recoverable_structured_error(
            AIRequestError('malformed', kind='invalid_json')
        ))
        self.assertFalse(is_recoverable_structured_error(
            AIRequestError('network down', kind='network_transport')
        ))
        self.assertFalse(is_recoverable_structured_error(
            AIRequestError('bad credentials', kind='authentication')
        ))
        self.assertFalse(is_recoverable_structured_error(RuntimeError('disk failure')))

    async def test_network_failure_during_segment_fallback_reaches_task_retry(self):
        config = {
            'ai_correction': True,
            'translation': 'off',
            'ai': {'correction_provider': {
                'transport': 'agy_cli',
                'cli_model': 'gemini-3.8-flash-high',
                'context_window': 256000,
                'concurrency': 1,
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            record = self._record(directory)
            calls = []

            async def flaky(items, *args, **kwargs):
                calls.append([item['id'] for item in items])
                if len(items) > 1:
                    raise AIRequestError('malformed JSON', kind='invalid_json')
                raise AIRequestError('network down', kind='network_transport')

            with patch('qwen_asr_app.ai.asyncio.sleep', new=AsyncMock()), \
                 patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=flaky):
                with self.assertRaises(AIRequestError) as caught:
                    await correct_batch_segments(
                        record,
                        list(record.segments),
                        config,
                    )

            self.assertEqual(caught.exception.kind, 'network_transport')
            self.assertEqual(len(calls), 14)  # 7 total attempts, each retries the batch then its first leaf
            self.assertTrue(all(call in ([1, 2], [1]) for call in calls))
            self.assertTrue(all(segment.get('ai_pending') for segment in record.segments))
            self.assertTrue(all(not segment.get('corrected') for segment in record.segments))

    async def test_successful_transport_retry_clears_old_segment_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self._record(directory)
            attempts = 0

            async def respond(items, *args, **kwargs):
                if len(items) > 1:
                    attempts += 1
                    if attempts == 1:
                        raise AIRequestError('malformed JSON', kind='invalid_json')
                    return [{'segment_ids': [item['id']], 'corrected': '精修' + item['raw']} for item in items]
                if items[0]['id'] == 1:
                    raise AIRequestError('empty response', kind='invalid_json')
                raise AIRequestError('network down', kind='network_transport')

            with patch('qwen_asr_app.ai.asyncio.sleep', new=AsyncMock()), \
                 patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=respond):
                result = await correct_batch_segments(record, list(record.segments), {})

            self.assertEqual(attempts, 2)
            self.assertEqual([item['corrected'] for item in result], ['精修原文1', '精修原文2'])
            self.assertTrue(all(not item.get('ai_error') for item in result))


if __name__ == '__main__':
    unittest.main()
