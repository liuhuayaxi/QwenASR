from tests import isolation  # noqa: F401

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.course_stream import CourseStreamJob, CourseStreamManager
from qwen_asr_app.storage import RecordingRecord


def model(transcribe):
    return SimpleNamespace(active_jobs=0, realtime_pending=True, cancel_unload=lambda: None,
                           schedule_unload=lambda: None, transcribe=transcribe)


class CourseResumeIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def paused_record(self, root, pause_on):
        record = RecordingRecord.create_live('静音断点', '未分类', root)
        record.metadata.update(status='processing', kind='upload',
                               course_stream={'chunks': [], 'completed_chunks': 0, 'expected_seconds': 75})
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def transcribe(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == pause_on:
                started.set()
                await release.wait()
                return '尚未提交的文字', 'Chinese', .01
            return ('' if calls == 1 else '已经保存的前文'), 'Chinese', .01

        job = CourseStreamJob(record, {}, model(transcribe), set())
        job.start()
        await job.accept(0, bytes(75 * 16000 * 2), {'samples': 75 * 16000})
        await job.finish_input(1)
        try:
            await asyncio.wait_for(started.wait(), 3)
        finally:
            stopping = asyncio.create_task(job.stop(paused=True))
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(stopping, 3)
        return RecordingRecord.load(record.directory)

    async def test_resume_preserves_text_after_silence_in_new_and_legacy_checkpoints(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as raw:
                saved = await self.paused_record(Path(raw), 3)
                self.assertEqual([(s['id'], s['raw']) for s in saved.segments], [(102, '已经保存的前文')])
                if legacy:
                    saved.metadata['course_stream']['chunks'][0].pop('transcribed_samples', None)
                    saved.save_all()
                    saved = RecordingRecord.load(saved.directory)
                transcribe = AsyncMock(return_value=('续跑得到的后文', 'Chinese', .01))
                job = CourseStreamJob(saved, {}, model(transcribe), set())
                job.start()
                await asyncio.wait_for(job.task, 10)
                loaded = RecordingRecord.load(saved.directory)
                self.assertEqual(loaded.metadata['status'], 'completed')
                self.assertEqual([(s['id'], s['raw'], s['start'], s['end']) for s in loaded.segments],
                                 [(102, '已经保存的前文', 25, 50), (103, '续跑得到的后文', 50, 75)])
                self.assertEqual(len(transcribe.call_args.args[0]), 25 * 16000)
                self.assertTrue(loaded.audio_path.is_file())

    async def test_silent_progress_survives_reload_without_repeating_audio(self):
        with tempfile.TemporaryDirectory() as raw:
            saved = await self.paused_record(Path(raw), 2)
            self.assertFalse(saved.segments)
            transcribe = AsyncMock(return_value=('静音之后的课堂内容', 'Chinese', .01))
            runtime = model(transcribe)
            runtime.realtime_pending = False
            job = CourseStreamJob(saved, {}, runtime, set())
            job.start()
            await asyncio.wait_for(job.task, 10)
            self.assertEqual(len(transcribe.call_args.args[0]), 50 * 16000)
            loaded = RecordingRecord.load(saved.directory)
            self.assertEqual(loaded.metadata['status'], 'completed')
            self.assertEqual(loaded.segments[0]['start'], 25)

    async def test_asr_only_works_without_llm_but_enabled_features_still_validate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            preferences = AIPreferences(root / 'ai.json')
            settings = SimpleNamespace(storage_root=root, add_category=lambda value: value)
            store = SimpleNamespace(paths={}, list_records=lambda: [])
            manager = CourseStreamManager(model(AsyncMock()), store, settings, set())
            data = {'key': 'asr-only', 'duration': 5, 'sources': [], 'config': {
                'ai_correction': False, 'generate_summary': False, 'summary_enabled': False, 'translation': 'off'}}
            with patch.dict(os.environ, {'QWEN_ASR_TESTING': '0'}), \
                    patch('qwen_asr_app.course_stream.ai_preferences', preferences):
                result = await manager.create(data)
                await manager.jobs[result['id']].stop()
                self.assertTrue(RecordingRecord.load(Path(result['directory'])).directory.exists())
                for flag, required in (('ai_correction', 'correction'), ('generate_summary', 'summary')):
                    with self.subTest(flag=flag), self.assertRaises(HTTPException) as raised:
                        await manager.create({**data, 'config': {**data['config'], flag: True}})
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertIn(required, raised.exception.detail)
                # A configured batch correction provider need not also have
                # the unrelated live-sentence provider configured.
                preferences.path.write_text(json.dumps({'function_providers': {
                    'correction': {'base_url': 'https://example.invalid/v1', 'model': 'synthetic'}}}))
                result = await manager.create({**data, 'config': {**data['config'], 'ai_correction': True}})
                await manager.jobs[result['id']].stop()
