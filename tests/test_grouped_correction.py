from tests import isolation  # noqa: F401

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from qwen_asr_app.ai import AIRequestError, polish_timestamped_segments
from qwen_asr_app.batch_ai import correct_batch_segments, segment_ai_complete
from qwen_asr_app.batch_pipeline import SegmentCorrectionPipeline
from qwen_asr_app.context_budget import estimate_tokens
from qwen_asr_app.storage import RecordingRecord
from qwen_asr_app.course_stream import CourseStreamJob
from types import SimpleNamespace

import numpy as np


def high_config():
    return {'ai_correction': True, 'translation': 'off', 'ai': {
        'correction_provider': {'transport': 'agy_cli', 'cli_model': 'gemini-3.8-flash-high',
                                'cli_effort': 'high', 'context_window': 256000, 'concurrency': 2},
    }}


def record_at(path):
    return RecordingRecord(path, {'id': 'synthetic-grouped', 'title': '合成课堂',
                                 'category': '测试课程', 'status': 'processing', 'sample_count': 0})


def segment(index, text=None):
    return {'id': index, 'raw': text or f'原文{index}', 'start': (index - 1) * 60,
            'end': index * 60, 'language': 'Chinese'}


async def correct(segments, language, config, **kwargs):
    return [{'segment_ids': [part['id']], 'corrected': '精修' + part['raw']} for part in segments]


class GroupedCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_course_stream_uses_grouped_correction_and_archives_the_last_batch(self):
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=correct) as request:
            record = record_at(Path(raw))
            record.metadata['course_stream'] = {'chunks': [], 'completed_chunks': 0, 'expected_seconds': 360}
            model = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                    transcribe=AsyncMock(return_value=('课堂原文', 'Chinese', .1)))
            job = CourseStreamJob(record, high_config(), model, set())
            try:
                job.start()
                block = np.full(60 * 16000, 500, dtype='<i2').tobytes()
                for index in range(6):
                    await job.accept(index, block, {'source': 0, 'samples': (index + 1) * 60 * 16000})
                await job.finish_input(6)
                await asyncio.wait_for(job.task, 8)
            finally:
                await job.stop()
            self.assertEqual(record.metadata['status'], 'completed')
            self.assertEqual([len(call.args[0]) for call in request.await_args_list], [6])
            self.assertEqual(job.snapshot()['corrected_segments'], 6)
            self.assertTrue(all(part.get('corrected') == '精修课堂原文' for part in record.segments))
            self.assertTrue(record.audio_path.is_file())
            self.assertEqual(model.active_jobs, 0)

    async def test_twelve_minutes_share_one_request_with_exact_segment_timestamps(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw))
            with patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=correct) as request:
                pipeline = SegmentCorrectionPipeline(record, high_config())
                try:
                    for index in range(1, 13):
                        await pipeline.submit(segment(index))
                    await asyncio.wait_for(pipeline.finish(), 2)
                finally:
                    await pipeline.close()
            self.assertEqual([len(call.args[0]) for call in request.await_args_list], [12])
            self.assertEqual([part['raw'] for part in record.segments], [f'原文{i}' for i in range(1, 13)])
            self.assertEqual([part['corrected'] for part in record.segments], [f'精修原文{i}' for i in range(1, 13)])
            self.assertEqual([(s['start'], s['end']) for s in record.segments], [(i * 60, (i + 1) * 60) for i in range(12)])
            for call in request.await_args_list:
                provider = call.args[2]['ai']['correction_provider']
                self.assertEqual(provider['cli_model'], 'gemini-3.8-flash-high')
                self.assertEqual(provider['cli_effort'], 'high')
            self.assertEqual(pipeline.events.qsize(), 12)

    async def test_next_batch_keeps_transcribing_while_first_batch_is_refining(self):
        entered, both, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def slow(parts, language, config, **kwargs):
            calls.append([part['id'] for part in parts])
            entered.set()
            if len(calls) == 2:
                both.set()
            await release.wait()
            return await correct(parts, language, config)

        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=slow):
            record = record_at(Path(raw))
            pipeline = SegmentCorrectionPipeline(record, high_config())
            pipeline.batch_seconds=300
            try:
                for index in range(1, 6):
                    await pipeline.submit(segment(index))
                await asyncio.wait_for(entered.wait(), 1)
                for index in range(6, 11):
                    await pipeline.submit(segment(index))
                await asyncio.wait_for(both.wait(), 1)
                self.assertEqual(len(record.segments), 10)
                self.assertTrue(all(not part.get('corrected') for part in record.segments))
                release.set()
                await asyncio.wait_for(pipeline.finish(), 2)
            finally:
                release.set()
                await pipeline.close()

    async def test_short_class_and_final_tail_are_flushed_at_finish(self):
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=correct) as request:
            record = record_at(Path(raw))
            pipeline = SegmentCorrectionPipeline(record, high_config())
            try:
                await pipeline.submit(segment(1))
                await asyncio.sleep(0)
                request.assert_not_awaited()
                await pipeline.finish()
                request.assert_awaited_once()
                self.assertTrue(segment_ai_complete(record.segments[0], high_config()))
            finally:
                await pipeline.close()

    async def test_dense_text_is_split_before_exceeding_the_source_budget(self):
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=correct) as request:
            pipeline = SegmentCorrectionPipeline(record_at(Path(raw)), high_config())
            try:
                for index in range(1, 6):
                    await pipeline.submit(segment(index, '公式推导' * 1800))
                await pipeline.finish()
                self.assertGreater(request.await_count, 1)
                for call in request.await_args_list:
                    self.assertLessEqual(sum(estimate_tokens(part['raw']) + 24 for part in call.args[0]), pipeline.batch_tokens)
            finally:
                await pipeline.close()

    async def test_interrupted_buffer_is_durable_and_resumes_without_losing_text(self):
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=correct) as request:
            record = record_at(Path(raw))
            pipeline = SegmentCorrectionPipeline(record, high_config())
            for index in range(1, 4):
                await pipeline.submit(segment(index))
            await pipeline.close()
            request.assert_not_awaited()
            loaded = RecordingRecord.load(record.directory)
            self.assertTrue(all(part.get('ai_pending') for part in loaded.segments))
            resumed = SegmentCorrectionPipeline(loaded, high_config())
            try:
                for part in list(loaded.segments):
                    if not segment_ai_complete(part, high_config()):
                        await resumed.submit(part)
                await resumed.finish()
            finally:
                await resumed.close()
            self.assertEqual([part['corrected'] for part in loaded.segments], ['精修原文1', '精修原文2', '精修原文3'])

    async def test_incomplete_mapping_is_not_partially_saved(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw))
            parts = [segment(1), segment(2)]
            for part in parts:
                record.upsert_segment(part)
            bad = [{'segment_ids': [1], 'corrected': '半份结果'}]
            with patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', new=AsyncMock(return_value=bad)):
                result = await correct_batch_segments(record, parts, high_config())
            self.assertTrue(all(part.get('ai_error') for part in result))
            self.assertTrue(all(not part.get('corrected') for part in result))
            self.assertEqual([part['raw'] for part in result], ['原文1', '原文2'])

    async def test_invalid_batch_result_falls_back_per_segment_without_blocking_siblings(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw))
            parts = [segment(1), segment(2), segment(3)]
            for part in parts:
                record.upsert_segment(part)

            async def flaky(items, *args, **kwargs):
                if len(items) > 1:
                    raise AIRequestError('malformed JSON', kind='invalid_json')
                if items[0]['id'] == 2:
                    raise AIRequestError('empty response', kind='invalid_json')
                return [{'segment_ids': [items[0]['id']], 'corrected': '精修' + items[0]['raw']}]

            with patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=flaky):
                result = await correct_batch_segments(record, parts, high_config())

            by_id = {part['id']: part for part in result}
            self.assertEqual(by_id[1]['corrected'], '精修原文1')
            self.assertEqual(by_id[3]['corrected'], '精修原文3')
            self.assertEqual(by_id[2]['ai_error'], 'empty response')
            self.assertFalse(by_id[1].get('ai_error'))
            self.assertFalse(by_id[3].get('ai_error'))

    async def test_successful_half_survives_cancel_and_resubmit_skips_it(self):
        from qwen_asr_app import ai
        waiting=asyncio.Event()
        async def split(parts,*args,**kwargs):
            if len(parts)>1:raise AIRequestError('bad mapping',kind='invalid_segment_mapping')
            if parts[0]['id']==2:
                waiting.set();await asyncio.Event().wait()
            return [{'segment_ids':[1],'corrected':'第一段已保存'}]
        with tempfile.TemporaryDirectory() as raw:
            record=record_at(Path(raw));parts=[segment(1),segment(2)]
            for item in parts:record.upsert_segment(item)
            with patch.object(ai,'polish_timestamped_segments',side_effect=split):
                task=asyncio.create_task(correct_batch_segments(record,parts,high_config()))
                await asyncio.wait_for(waiting.wait(),1);task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            loaded=RecordingRecord.load(record.directory)
            self.assertEqual(loaded.segments[0]['corrected'],'第一段已保存')
            with patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient',side_effect=correct) as request:
                pipeline=SegmentCorrectionPipeline(loaded,high_config())
                try:
                    for part in loaded.segments:await pipeline.submit(part)
                    await pipeline.finish()
                finally:await pipeline.close()
                self.assertEqual([s['id'] for s in request.await_args.args[0]],[2])

    async def test_successful_batch_clears_old_network_errors(self):
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=correct):
            record = record_at(Path(raw))
            part = {**segment(1), 'ai_error': '旧网络错误'}
            record.upsert_segment(part)
            await correct_batch_segments(record, [part], high_config())
            self.assertNotIn('ai_error', record.segments[0])
            self.assertFalse(record.segments[0]['ai_pending'])

    async def test_rejects_merged_results_when_each_timestamp_needs_its_own_text(self):
        config = high_config()
        merged = {'blocks': [{'segment_ids': [1, 2], 'corrected': '整批文本'}]}
        with patch('qwen_asr_app.ai.request_ai_json', new=AsyncMock(return_value=merged)):
            with self.assertRaises(AIRequestError) as caught:
                await polish_timestamped_segments([segment(1), segment(2)], 'Chinese', config, mode='correction')
        self.assertEqual(caught.exception.kind, 'invalid_segment_mapping')


if __name__ == '__main__':
    unittest.main()
