from tests import isolation  # Must precede application imports.

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app
from qwen_asr_app.storage import RecordingRecord, RecordingStore


class SummaryNamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_treats_non_mapping_summary_as_missing_without_rewriting_it(self):
        for saved in [{'summary': 'previous valid note'}, 'legacy summary', '', None, [], ['invalid'], 42, {},
                      {'summary': 'saved note', 'review_status': 'approved'}]:
            with self.subTest(saved=saved):
                self.record.metadata['ai_note_summary'] = saved
                self.record.save_metadata()
                result = await app.summary_status(self.record.id)
                valid = saved if isinstance(saved, dict) else {}
                self.assertEqual(result['has_summary'], bool(valid.get('summary')))
                self.assertEqual(result['review_status'], valid.get('review_status', 'unreviewed'))
                self.assertEqual(self.store.find(self.record.id).metadata['ai_note_summary'], saved)
                self.assertFalse(app._summary_abstract_pending(self.store.find(self.record.id)))

    async def test_create_summary_handles_non_mapping_saved_note(self):
        self.record.metadata['ai_note_summary'] = 'legacy summary'
        self.record.save_metadata()
        with patch.object(app, 'start_summary_job') as start:
            result = await app.create_summary_job(self.record.id, {'config': {}})
        self.assertTrue(result['accepted'])
        self.assertFalse(start.call_args.args[1]['regenerate'])

    async def test_edit_replaces_non_mapping_saved_note(self):
        self.record.metadata['ai_note_summary'] = 'legacy summary'
        self.record.save_metadata()
        result = await app.save_recording_summary(self.record.id,
            {'summary': 'Replacement note', 'expected_summary': ''})
        self.assertEqual(result['summary']['summary'], 'Replacement note')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.record = RecordingRecord(root / "original", {
            "id": "summary-title-test", "title": "原标题", "category": "光电子学",
            "status": "completed", "started_at": "2026-10-07T10:00:00+08:00",
            "sample_count": 16000,
        })
        self.record.segments = [{"id": 1, "start": 0, "end": 1, "raw": "光纤的归一化频率。"}]
        self.record.save_all()
        self.store = RecordingStore.__new__(RecordingStore)
        self.store.active = None
        self.store.paths = {self.record.id: self.record.directory}
        self.title = AsyncMock(return_value="光纤归一化频率")
        self.summary = AsyncMock(return_value={"summary": "## 知识点\n\n光纤的归一化频率。"})
        self.abstract = AsyncMock(return_value="光纤模式与归一化频率。")
        self.preferences = {"title_prompt": "使用主题取名"}
        for context in (
            patch.object(app, "recording_store", self.store),
            patch.object(app.app_settings, "storage_root", root),
            patch.object(app.app_settings, "library_layout", "", create=True),
            patch.object(app.app_settings, "add_category", side_effect=lambda value: value),
            patch.object(app.ai_preferences, "read", return_value=self.preferences),
            patch.object(app, "generate_recording_title", self.title),
            patch("qwen_asr_app.batch_ai.summarize_transcript", self.summary),
            patch("qwen_asr_app.batch_ai.summarize_note_abstract", self.abstract),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.payload = {"config": {"ai": {"summary_provider": {"model": "mock-summary"}}}}

    async def run_job(self):
        app.start_summary_job(self.store.find(self.record.id), self.payload, automatic=True)
        await app.background_summary_tasks[self.record.id]
        return self.store.find(self.record.id)

    async def test_legacy_manual_post_with_existing_note_requests_fresh_generation(self):
        self.record.metadata['ai_note_summary']={'summary':'旧版正文'}
        self.record.save_metadata()
        with patch.object(app,'start_summary_job') as start:
            await app.create_summary_job(self.record.id,{'config':{}})
            self.assertTrue(start.call_args.args[1]['regenerate'])
            await app.create_summary_job(self.record.id,{'config':{},'regenerate':False})
            self.assertFalse(start.call_args.args[1]['regenerate'])

    async def test_generation_round_survives_auto_retry_and_continue_but_manual_regenerate_changes_it(self):
        from qwen_asr_app.ai import AIRequestError
        seen=[]
        self.record.metadata['summary_job']={'status':'completed','checkpoint':{'review_task':'a'*24}}
        self.record.save_metadata()
        async def generation(ident,payload):
            seen.append(payload['_note_generation_id'])
            if len(seen)==1:
                raise AIRequestError('temporary upstream failure',kind='provider_unavailable',retriable=True)
            return {'summary':{'summary':'完整新版正文','abstract':'摘要'}}
        with patch.object(app,'generate_library_summary',side_effect=generation), \
             patch.object(app.asyncio,'sleep',new=AsyncMock()):
            app.start_summary_job(self.store.find(self.record.id),{'config':{},'regenerate':True})
            first=app.background_summary_tasks[self.record.id]
            self.assertEqual(self.store.find(self.record.id).metadata['summary_job']['checkpoint'],{})
            await first
            self.assertEqual(len(seen),2);self.assertEqual(seen[0],seen[1]);self.assertTrue(seen[0])
            app.start_summary_job(self.store.find(self.record.id),{'config':{}})
            await app.background_summary_tasks[self.record.id]
            self.assertEqual(seen[2],seen[0])
            app.start_summary_job(self.store.find(self.record.id),{'config':{},'regenerate':True})
            await app.background_summary_tasks[self.record.id]
            self.assertNotEqual(seen[3],seen[0])

    async def test_background_job_saves_name_after_note_and_moves_record_safely(self):
        self.payload["title_prompt"] = "按本次设置取名"
        async def title(summary, prompt, config, **context):
            saved = self.store.find(self.record.id)
            self.assertEqual(saved.metadata["ai_note_summary"]["summary"], summary)
            self.assertTrue(saved.ai_summary_path.exists())
            self.assertEqual(prompt, "按本次设置取名")
            self.assertEqual(config["ai"]["summary_provider"]["model"], "mock-summary")
            self.assertEqual(context["category"], "光电子学")
            self.assertEqual((await app.summary_status(saved.id))["phase"], "AI 取名")
            return "光纤归一化频率"
        self.title.side_effect = title
        saved = await self.run_job()
        self.assertEqual(saved.metadata["title"], "光纤归一化频率")
        self.assertEqual(saved.metadata["category"], "光电子学")
        # Independent note review is off unless explicitly saved as enabled, so a
        # text-provider note is complete rather than "unreviewed".
        self.assertEqual(saved.metadata["summary_job"]["status"], "completed")
        self.assertEqual(saved.metadata['ai_note_summary']['review_status'],'skipped')
        self.assertEqual(saved.metadata['ai_note_summary']['review_policy'],'disabled_by_settings')
        self.assertTrue(saved.ai_summary_path.exists())
        self.assertFalse(self.record.directory.exists())
        self.title.assert_awaited_once()

    async def test_explicitly_enabled_review_marks_text_note_unreviewed(self):
        with patch.object(app.ai_preferences, "task_config",
                          side_effect=lambda config, **_: {**(config or {}), "note_review_enabled": True}):
            saved = await self.run_job()
        self.assertEqual(saved.metadata["summary_job"]["status"], "completed_with_errors")
        self.assertEqual(saved.metadata['ai_note_summary']['review_status'],'unreviewed')

    async def test_direct_generation_uses_saved_prompt_then_default(self):
        result = await app.generate_library_summary(self.record.id, self.payload)
        self.assertEqual(result["session"]["title"], "光纤归一化频率")
        self.assertEqual(self.title.await_args.args[1], "使用主题取名")
        self.preferences.clear()
        await app.generate_library_summary(self.record.id, self.payload)
        self.assertEqual(self.title.await_args.args[1], app.DEFAULT_AI_TITLE_PROMPT)

    async def test_text_provider_note_assigns_chapter_from_saved_lesson_selection(self):
        self.record.metadata['material_selection'] = {'files': [
            {'source_id': 'slides', 'name': '第二章 光纤导光原理.pdf', 'chosen': True},
            {'source_id': 'other', 'name': '第三章 光纤损耗.pdf', 'chosen': False},
        ]}
        self.record.save_metadata()
        result = await app.generate_library_summary(self.record.id, self.payload)
        self.assertEqual(result['session']['chapters'], ['第2章 光纤导光原理'])
        self.assertEqual(self.store.find(self.record.id).metadata['chapter_info']['source'], 'materials')

    async def test_title_failure_preserves_note_and_manual_retry_clears_error(self):
        self.title.side_effect = RuntimeError("取名服务暂时失败")
        saved = await self.run_job()
        # A naming failure keeps the note; it is recorded as title_error and the
        # job itself is complete (review disabled by default settings).
        self.assertEqual(saved.metadata["summary_job"]["status"], "completed")
        self.assertEqual(saved.metadata["title"], "原标题")
        self.assertEqual(saved.metadata["ai_note_summary"]["title_error"], "取名服务暂时失败")
        self.assertIn("归一化频率", saved.ai_summary_path.read_text())
        self.summary.assert_awaited_once()
        self.title.side_effect = None
        result = await app.generate_title(saved.id, {**self.payload, "apply": True})
        self.assertEqual(result["session"]["title"], "光纤归一化频率")
        self.assertNotIn("title_error", result["session"]["ai_note_summary"])
        self.assertNotIn("title_error", self.store.find(saved.id).metadata["ai_note_summary"])

    async def test_abstract_failure_does_not_skip_naming(self):
        self.abstract.side_effect = RuntimeError("短摘要失败")
        saved = await self.run_job()
        self.assertEqual(saved.metadata["title"], "光纤归一化频率")
        self.assertEqual(saved.metadata["ai_note_summary"]["abstract_error"], "短摘要失败")
        self.title.assert_awaited_once()

    async def test_failed_note_never_starts_naming(self):
        self.summary.side_effect = RuntimeError("详细笔记失败")
        saved = await self.run_job()
        self.assertEqual(saved.metadata["summary_job"]["status"], "failed")
        self.assertEqual(saved.metadata["title"], "原标题")
        self.title.assert_not_awaited()

    async def test_duplicate_submission_during_naming_reuses_running_job(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def title(*args, **kwargs):
            entered.set()
            await release.wait()
            return "光纤归一化频率"
        self.title.side_effect = title
        app.start_summary_job(self.record, self.payload)
        task = app.background_summary_tasks[self.record.id]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            app.start_summary_job(self.store.find(self.record.id), self.payload)
            self.assertIs(app.background_summary_tasks[self.record.id], task)
        finally:
            release.set()
            await task
        self.summary.assert_awaited_once()
        self.title.assert_awaited_once()
