from __future__ import annotations

from tests import isolation  # Must precede application imports.

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np

import app
from qwen_asr_app.ai import extract_chat_text, extract_output_text, request_ai_text, test_ai_connection
from qwen_asr_app.batch_ai import build_note_summary, global_rewrite_events, note_summary_chunks
from qwen_asr_app.batch_pipeline import SegmentCorrectionPipeline
from qwen_asr_app.note_content import compose_push, summary_from_archive, transcript_blocks
from qwen_asr_app.storage import RecordingRecord, RecordingStore


def record_at(path, **metadata):
    return RecordingRecord(path, {
        "id": "upload-20260908-100000-testqa", "title": "通信课堂", "category": "通信原理",
        "kind": "upload", "status": "completed", "sample_count": 48000,
        "audio_filename": "recording.aac", "started_at": "2026-09-08T10:00:00+08:00",
        "ai_note_summary": {"summary": "# 课堂笔记\n\n## 知识点速览\n\n香农公式。", "abstract": ""},
        **metadata,
    })


class ArchiveTests(unittest.TestCase):
    def test_timestamped_review_covers_members_once_and_keeps_unfinished_tail(self):
        record = record_at(Path('/tmp'), ai_reviews=[
            {"start": 0, "end": 2, "segment_ids": [1, 2], "corrected": "合并精修"}])
        record.segments = [{"id": i, "start": i-1, "end": i, "raw": f"原文{i}"} for i in range(1, 4)]
        blocks = transcript_blocks(record)
        self.assertEqual([b['text'] for b in blocks], ['合并精修', '原文3'])
        archive = compose_push(record)
        self.assertLess(archive['content'].index('## AI 总结'), archive['content'].index('## 精修录音原文'))
        self.assertIn('00:00:00–00:00:02', archive['content'])
        self.assertEqual(archive['unrefined_blocks'], 1)
        self.assertNotIn('原文1', archive['content'])
        self.assertEqual(summary_from_archive(archive['content'], {'content_mode': 'combined'}),
                         record.metadata['ai_note_summary']['summary'])

    def test_transcript_only_without_any_summary_and_summary_only_without_transcript(self):
        record = record_at(Path('/tmp'), ai_note_summary={})
        record.segments = [{'id': 1, 'start': 0, 'end': 3, 'raw': '原文', 'corrected': '精修原文'}]
        text = compose_push(record, 'transcript')['content']
        self.assertIn('精修原文', text)
        self.assertNotIn('## AI 总结', text)
        with self.assertRaisesRegex(ValueError, '没有可识别'):
            summary_from_archive(text, {'content_mode': 'transcript'})
        record = record_at(Path('/tmp'))
        self.assertNotIn('精修录音原文', compose_push(record, 'summary')['content'])

    def test_incomplete_global_rewrite_does_not_hide_later_segments(self):
        record = record_at(Path('/tmp'), ai_global_rewrite={'complete': False, 'text': '半份稿',
            'parts': [{'segment_ids': [1], 'corrected': '半份稿'}]})
        record.segments = [{'id': i, 'start': i-1, 'end': i, 'raw': f'完整{i}'} for i in (1, 2)]
        self.assertEqual([b['text'] for b in transcript_blocks(record)], ['完整1', '完整2'])

    def test_library_refresh_keeps_live_record_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw), kind='microphone', status='active')
            record.save_all()
            store = RecordingStore.__new__(RecordingStore)
            store.active, store.paths = record, {}
            store._metadata_paths = lambda: [record.meta_path]
            store.refresh()
            self.assertIs(store.active, record)

    def test_loading_record_does_not_delete_existing_markdown(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw))
            record.save_all()
            record.notes_path.write_text('用户旧笔记')
            RecordingRecord.load(record.directory)
            self.assertTrue(record.notes_path.exists())

    def test_truncated_outputs_fail_instead_of_becoming_saved_notes(self):
        with self.assertRaisesRegex(RuntimeError, '未完成'):
            extract_output_text({'status': 'incomplete', 'output_text': '半份总结'})
        with self.assertRaisesRegex(RuntimeError, '截断'):
            extract_chat_text({'choices': [{'finish_reason': 'length', 'message': {'content': '半份总结'}}]})


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_resume_reuses_successful_subranges_after_a_sibling_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            record=record_at(Path(raw),ai_note_summary={})
            record.segments=[{'id':i,'start':(i-1)*60,'end':i*60,'raw':f'原文{i}'} for i in (1,2,3)]
            config={'ai_correction':True,'translation':'off','ai':{'global_provider':{'context_window':256000}}}
            async def partial(segments,*args,**kwargs):
                kwargs['on_checkpoint']([{'segment_ids':[1],'corrected':'已保存第一段'}])
                raise RuntimeError('second range disconnected')
            with patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient',side_effect=partial):
                events=[event async for event in global_rewrite_events(record,config)]
            self.assertFalse(events[-1]['global_rewrite']['complete'])
            loaded=RecordingRecord.load(record.directory)
            self.assertEqual(loaded.metadata['ai_global_rewrite']['parts'][0]['blocks'][0]['corrected'],'已保存第一段')
            async def finish(segments,*args,**kwargs):
                self.assertEqual([s['id'] for s in segments],[2,3])
                return [{'segment_ids':[s['id']],'corrected':f"完成{s['id']}"} for s in segments]
            with patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient',side_effect=finish):
                events=[event async for event in global_rewrite_events(loaded,config)]
            self.assertTrue(events[-1]['global_rewrite']['complete'])
            self.assertEqual([b['text'] for b in transcript_blocks(loaded)],['已保存第一段','完成2','完成3'])

    async def test_global_pass_shares_one_request_but_persists_independent_segments_and_translations(self):
        with tempfile.TemporaryDirectory() as raw:
            record=record_at(Path(raw),ai_note_summary={})
            record.segments=[{'id':i,'start':(i-1)*60,'end':i*60,'raw':f'原文{i}'} for i in (1,2,3)]
            async def respond(segments,*args,**kwargs):
                self.assertEqual([s['id'] for s in segments],[1,2,3])
                return [{'segment_ids':[s['id']],'corrected':f"精修{s['id']}",
                    'translation':f"Translation {s['id']}",'translation_target':'English'} for s in segments]
            config={'ai_correction':True,'translation':'en','ai':{'global_provider':{'context_window':256000,'concurrency':2}}}
            with patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient',new=AsyncMock(side_effect=respond)) as request:
                events=[event async for event in global_rewrite_events(record,config)]
            request.assert_awaited_once();self.assertTrue(events[-1]['global_rewrite']['complete'])
            loaded=RecordingRecord.load(record.directory);blocks=transcript_blocks(loaded)
            self.assertEqual([(b['segment_ids'],b['start'],b['end']) for b in blocks],[([1],0,60),([2],60,120),([3],120,180)])
            self.assertEqual([b['translation'] for b in blocks],['Translation 1','Translation 2','Translation 3'])
            self.assertIn('00:01:00–00:02:00',loaded.corrected_transcript_path.read_text())
            self.assertEqual([b['text'] for b in blocks],['精修1','精修2','精修3'])

    async def test_next_asr_starts_before_previous_ai_finishes(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw), status='interrupted', ai_note_summary={})
            record.audio_path.touch()
            record.complete = lambda: record.metadata.update(status='completed')
            next_asr = asyncio.Event()
            order = []
            calls = 0
            async def transcribe(*args, **kwargs):
                nonlocal calls
                calls += 1
                order.append(f'asr{calls}')
                if calls == 2: next_asr.set()
                await asyncio.sleep(.01)
                return f'课堂段{calls}', 'Chinese', .01
            async def correct(rec, segment, config):
                if segment['id'] == 1:
                    await next_asr.wait()
                order.append(f'ai{segment["id"]}')
                segment.update(corrected='精修' + segment['raw'], ai_pending=False)
                rec.upsert_segment(segment)
                return segment
            async def global_events(rec, config):
                self.assertTrue(all(s.get('corrected') for s in rec.segments))
                yield {'type': 'ai_global_complete', 'global_rewrite': {'complete': True}}
            manager = SimpleNamespace(active_jobs=0, cancel_unload=lambda: None, schedule_unload=lambda: None,
                                      load=AsyncMock(), transcribe=transcribe)
            with patch.object(app, 'model_manager', manager), patch.object(app, 'decode_audio_file', return_value=np.zeros(48000)), \
                 patch.object(app, 'vad_split_ranges', return_value=[(0,16000),(16000,32000),(32000,48000)]), \
                 patch('qwen_asr_app.batch_pipeline.correct_batch_segment', side_effect=correct), \
                 patch.object(app, 'global_rewrite_events', global_events):
                async def collect():
                    return [json.loads(e) async for e in app.batch_transcription_stream(None, 'test.wav',
                        {'ai_correction': True, 'asr_params': {}, 'translation': 'off'}, existing_record=record)]
                events = await asyncio.wait_for(collect(), 3)
            self.assertEqual(events[-1]['type'], 'complete', events)
            self.assertLess(order.index('asr2'), order.index('ai1'))
            self.assertEqual(len(record.segments), 3)
            self.assertEqual(len([e for e in events if e['type'] == 'ai_segment']), 3)
            self.assertEqual(manager.active_jobs, 0)
            self.assertNotIn(record.id, app.running_batch_ids)

    async def test_consumer_error_does_not_deadlock_queue(self):
        with patch('qwen_asr_app.batch_pipeline.correct_batch_segment', side_effect=OSError('磁盘写入失败')):
            pipeline = SegmentCorrectionPipeline(None, {})
            try:
                with self.assertRaisesRegex(OSError, '磁盘'):
                    await pipeline.submit({'id': 1})
                    await asyncio.sleep(.01)
                    await asyncio.wait_for(pipeline.submit({'id': 2}), .5)
            finally:
                await pipeline.close()

    async def test_long_summary_keeps_every_chunk_and_uses_summary_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw))
            record.segments = [{'id': i, 'start': i*60, 'end': (i+1)*60,
                                'raw': f'第{i}节。' + '完整推导例题。'*1200} for i in range(5)]
            calls = []
            async def summarize(chunk, previous, config, **kwargs):
                calls.append((chunk, previous, kwargs['provider_mode']))
                n = len(calls)
                return {'summary': f'## 知识点速览\n\n知识{n}\n\n## 公式速览\n\n公式{n}\n\n## 按时间展开\n\n详细推导{n}'}
            with patch('qwen_asr_app.batch_ai.summarize_transcript', side_effect=summarize), \
                 patch('qwen_asr_app.batch_ai.summarize_note_abstract', return_value='全部课程知识'):
                result = await build_note_summary(record, {'ai': {'summary_provider': {'context_window': 32768}}})
            self.assertGreater(len(calls), 1)
            self.assertTrue(all(previous == '' and mode == 'summary' for _,previous,mode in calls))
            self.assertIn('详细推导1', result['summary'])
            self.assertIn(f'详细推导{len(calls)}', result['summary'])
            for i in range(5): self.assertIn(f'第{i}节', ''.join(c[0] for c in calls))

    def test_agy_gemini_uses_long_context_before_chunking(self):
        blocks = ["[来源 S1] [00:00:00–01:00:00]\n" + "长课堂内容。" * 6_000]
        config = {'ai': {'summary_provider': {
            'transport': 'agy_cli', 'cli_model': 'gemini-3.8-flash-high',
            'context_window': 128_000, 'max_output_tokens': 16_384,
        }}}
        chunks = note_summary_chunks(blocks, config)
        self.assertEqual(len(chunks), 1)

    def test_agy_gemini_still_splits_when_saved_context_is_exceeded(self):
        blocks = ['完整课堂原文。' * 2_000 for _ in range(3)]
        config = {'ai': {'summary_provider': {
            'transport': 'agy_cli', 'cli_model': 'gemini-3.8-flash-high',
            'context_window': 32_768, 'max_output_tokens': 4096,
        }}}
        chunks = note_summary_chunks(blocks, config)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks).replace('\n', ''), ''.join(blocks))

    async def test_agy_gemini_refinement_keeps_a_normal_lecture_in_one_group(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw))
            record.segments = [{'id': i + 1, 'start': i * 60, 'end': (i + 1) * 60,
                                'raw': '课堂原文内容。' * 25} for i in range(60)]
            async def refine(segments, *args, **kwargs):
                return [{'segment_ids': [s['id']], 'corrected': s['raw']} for s in segments]
            with patch.object(app, 'polish_timestamped_segments_resilient', new=AsyncMock(side_effect=refine)) as call:
                await app._run_refinement_job_unlocked(record, {'ai': {'correction_provider': {
                    'transport': 'agy_cli', 'cli_model': 'gemini-3.8-flash-high',
                    'context_window': 256_000, 'max_output_tokens': 8_000,
                }}})
            call.assert_awaited_once()
            self.assertEqual(len(call.await_args.args[0]), 60)

    async def test_provider_test_uses_selected_credentials_and_reports_real_reply(self):
        requests = []
        async def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok": true}'}}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch('qwen_asr_app.ai.httpx.AsyncClient', return_value=client):
            result = await test_ai_connection({'ai': {'review_provider': {
                'base_url': 'http://localhost:8999/v1', 'api_key': 'test-private-key',
                'model': 'test-model', 'api_mode':'chat_completions', 'context_window':32768}}}, 'review')
        self.assertTrue(result['ok'])
        self.assertEqual(requests[0].headers['Authorization'], 'Bearer test-private-key')
        self.assertEqual(json.loads(requests[0].content)['model'], 'test-model')
        self.assertNotIn('enable_thinking', json.loads(requests[0].content))
        self.assertNotIn('test-private-key', json.dumps(result))

    async def test_api_preview_rejects_changed_content_before_remote_write(self):
        record = record_at(Path('/tmp'))
        with patch.object(app.recording_store, 'find', return_value=record), \
             patch.object(app.ideashell_manager, 'connection') as connection:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                response = await client.post(f'/api/recordings/{record.id}/ideashell/push',
                    json={'content_mode':'summary','preview_digest':'stale'})
        self.assertEqual(response.status_code, 400)
        connection.assert_not_called()


class FunctionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_parallelism_is_bounded_and_output_keeps_source_order(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw))
            record.segments = [{'id':i,'start':i,'end':i+1,'raw':f'chunk-{i}'} for i in range(6)]
            active = peak = 0
            async def summarize(chunk, previous, config, **kw):
                nonlocal active, peak
                active += 1; peak = max(peak, active)
                n=int(chunk)
                await asyncio.sleep(.03 if n % 2 == 0 else .005)
                active -= 1
                return {'summary':f'## 知识点速览\n知识{n}\n## 公式速览\n公式{n}\n## 按时间展开\n细节{n}'}
            with patch('qwen_asr_app.batch_ai.note_summary_chunks',return_value=[str(i) for i in range(6)]), \
                 patch('qwen_asr_app.batch_ai.summarize_transcript',side_effect=summarize), \
                 patch('qwen_asr_app.batch_ai.summarize_note_abstract',return_value='课程知识'):
                result=await build_note_summary(record,{'ai':{'summary_provider':{'concurrency':3}}})
            self.assertEqual(peak,3)
            positions=[result['summary'].index(f'细节{i}') for i in range(6)]
            self.assertEqual(positions, sorted(positions))

    async def test_global_rewrite_retry_reuses_successful_parts_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw), ai_note_summary={})
            record.segments = [
                {'id': i + 1, 'start': i, 'end': i + 1, 'raw': f'chunk-{i}', 'language': 'Chinese'}
                for i in range(3)
            ]
            first_calls = []

            async def first_pass(segments, language, config, **kwargs):
                index = segments[0]['id']-1
                first_calls.append(index)
                if index == 1:
                    raise RuntimeError('temporary failure')
                await asyncio.sleep(.01 if index == 0 else .001)
                return [{'segment_ids':[segments[0]['id']], 'corrected': f'edited-{index}'}]

            config = {'ai_correction': True, 'translation': 'off',
                      'ai': {'global_provider': {'concurrency': 2, 'context_window': 32768}}}
            with patch('qwen_asr_app.batch_ai.global_source_groups',
                       return_value=[[segment] for segment in record.segments]), \
                 patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=first_pass):
                first_events = [event async for event in global_rewrite_events(record, config)]
            self.assertFalse(first_events[-1]['global_rewrite']['complete'])
            self.assertEqual(first_calls, [0, 1, 2])

            retry_calls = []

            async def retry_pass(segments, language, retry_config, **kwargs):
                index = segments[0]['id']-1
                retry_calls.append(index)
                return [{'segment_ids':[segments[0]['id']], 'corrected': f'edited-{index}'}]

            with patch('qwen_asr_app.batch_ai.global_source_groups',
                       return_value=[[segment] for segment in record.segments]), \
                 patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient', side_effect=retry_pass):
                retry_events = [event async for event in global_rewrite_events(record, config)]
            result = retry_events[-1]['global_rewrite']
            self.assertEqual(retry_calls, [1])
            self.assertTrue(result['complete'])
            self.assertEqual(result['text'], 'edited-0\n\nedited-1\n\nedited-2')

    async def test_global_rewrite_cancel_collects_finished_unreported_parts(self):
        with tempfile.TemporaryDirectory() as raw:
            record = record_at(Path(raw), ai_note_summary={})
            record.segments = [
                {'id': i + 1, 'start': i, 'end': i + 1, 'raw': f'chunk-{i}', 'language': 'Chinese'}
                for i in range(3)
            ]
            workers_finished = asyncio.Event()
            completed = 0

            async def finish_without_yielding(segments, language, config, **kwargs):
                nonlocal completed
                completed += 1
                if completed == 3:
                    workers_finished.set()
                return [{'segment_ids':[segments[0]['id']], 'corrected': f"edited-{segments[0]['id']-1}"}]

            async def stalled_result():
                await asyncio.Event().wait()

            config = {'ai_correction': True, 'translation': 'off',
                      'ai': {'global_provider': {'concurrency': 3, 'context_window': 32768}}}
            with patch('qwen_asr_app.batch_ai.global_source_groups',
                       return_value=[[segment] for segment in record.segments]), \
                 patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient',
                       side_effect=finish_without_yielding), \
                 patch('qwen_asr_app.batch_ai.asyncio.as_completed',
                       side_effect=lambda tasks: iter([stalled_result()])):
                collecting = asyncio.create_task(
                    anext(global_rewrite_events(record, config))
                )
                await asyncio.wait_for(workers_finished.wait(), 1)
                await asyncio.sleep(0)
                collecting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await collecting

            saved = record.metadata['ai_global_rewrite']
            self.assertFalse(saved['complete'])
            self.assertEqual(saved['text'], 'edited-0\n\nedited-1\n\nedited-2')
            self.assertEqual(len(saved['parts']), 3)

            with patch('qwen_asr_app.batch_ai.global_source_groups',
                       return_value=[[segment] for segment in record.segments]), \
                 patch('qwen_asr_app.batch_ai.polish_timestamped_segments_resilient') as retry:
                events = [event async for event in global_rewrite_events(record, config)]
            retry.assert_not_called()
            self.assertTrue(events[-1]['global_rewrite']['complete'])

    async def test_rate_limit_is_retried_before_success(self):
        attempts = 0

        async def respond(request):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429, headers={'retry-after': '0'}, json={'error': 'slow down'})
            return httpx.Response(200, json={
                'choices': [{'finish_reason': 'stop', 'message': {'content': 'done'}}]
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch('qwen_asr_app.ai.httpx.AsyncClient', return_value=client):
            result = await request_ai_text(
                instructions='test', payload={'value': 1}, mode='summary', max_output_tokens=128,
                config={'ai': {'summary_provider': {
                    'base_url': 'http://mock/v1', 'model': 'summary-model',
                    'api_mode': 'chat_completions', 'context_window': 32768,
                    'extra_params': {}, 'concurrency': 3,
                }}},
            )
        self.assertEqual(result, 'done')
        self.assertEqual(attempts, 3)

    async def test_cancel_releases_summary_lock_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            record=record_at(Path(raw));record.segments=[{'id':1,'start':0,'end':1,'raw':'课程'}]
            entered=asyncio.Event()
            async def blocking(*args):
                entered.set(); await asyncio.Event().wait()
            with patch.object(app.recording_store,'find',return_value=record), \
                 patch.object(app,'build_note_summary',side_effect=blocking):
                task=asyncio.create_task(app.generate_library_summary(record.id,{'config':{'ai':{}}}))
                await asyncio.wait_for(entered.wait(),1)
                self.assertTrue((await app.summary_status(record.id))['running'])
                await app.cancel_summary(record.id)
                with self.assertRaises(app.HTTPException): await task
                self.assertNotIn(record.id,app.record_operations)
                self.assertFalse((await app.summary_status(record.id))['running'])

    def test_independent_refinement_and_global_model_and_redaction(self):
        from qwen_asr_app.ai import provider_for_mode
        ai={'model':'live','api_key':'live-key',
            'summary_provider':{'model':'notes','api_key':'notes-key'},
            'global_provider':{'model':'global','api_key':'global-key'},
            'correction_provider':{'model':'polish','api_key':'polish-key'}}
        self.assertEqual(provider_for_mode(ai,'summary')['model'],'notes')
        self.assertEqual(provider_for_mode(ai,'global')['model'],'global')
        self.assertEqual(provider_for_mode(ai,'correction')['model'],'polish')
        self.assertNotIn('-key',json.dumps(app.redacted_batch_config({'ai':ai})))


if __name__ == '__main__':
    unittest.main()
