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


if __name__ == '__main__':
    unittest.main()
