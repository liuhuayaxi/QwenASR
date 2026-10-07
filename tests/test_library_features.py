from __future__ import annotations

from tests import isolation  # Must precede application imports.

import tempfile
import time
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from qwen_asr_app.ai import (
    context_window_for,
    extract_chat_text,
    generate_recording_title,
    polish_and_translate,
    provider_for_mode,
    request_ai_text,
    summarize_note_abstract,
    summarize_transcript,
)
from qwen_asr_app.batch_ai import build_note_summary
from qwen_asr_app.storage import AppSettings, RecordingRecord
from qwen_asr_app.ideashell_sync import FileTokenStorage, IdeaShellConnection
from app import ensure_ideashell_course_folder, ideashell_folder_id, ideashell_folders


class StorageMigrationTests(unittest.TestCase):
    def make_settings(self, source: Path) -> AppSettings:
        settings = AppSettings.__new__(AppSettings)
        settings.storage_root = source
        settings.categories = ["未分类", "通信原理"]
        settings.known_roots = []
        settings.category_profiles = {}
        settings.save = lambda: None
        return settings

    def test_migration_moves_qwen_files_but_not_unrelated_documents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "Documents"
            target = base / "Library"
            course = source / "通信原理"
            record = course / "第三讲__20260908-100000-abcdef"
            record.mkdir(parents=True)
            (record / "session.json").write_text(
                '{"id":"20260908-100000-abcdef","status":"completed"}', encoding="utf-8"
            )
            (course / ".qwenasr-course.json").write_text("{}", encoding="utf-8")
            unrelated = source / "不要动.txt"
            unrelated.write_text("keep", encoding="utf-8")

            result = self.make_settings(source).migrate_storage_root(str(target))

            self.assertTrue((target / "通信原理" / record.name / "session.json").exists())
            self.assertTrue(unrelated.exists())
            self.assertFalse(record.exists())
            self.assertEqual(result["migrated_records"], 1)

    def test_migration_allows_a_dedicated_folder_inside_documents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "Documents"
            record = source / "通信原理" / "lesson__20260908-100000-abcdef"
            record.mkdir(parents=True)
            (record / "session.json").write_text(
                '{"id":"20260908-100000-abcdef"}', encoding="utf-8"
            )
            target = source / "拾课资料库"

            result = self.make_settings(source).migrate_storage_root(str(target))

            self.assertEqual(result["storage_root"], str(target.resolve()))
            self.assertTrue((target / "通信原理" / record.name / "session.json").exists())

    def test_migration_rejects_course_nested_root_and_existing_collision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "Documents"
            source.mkdir()
            settings = self.make_settings(source)
            with self.assertRaises(ValueError):
                settings.migrate_storage_root(str(source / "通信原理" / "nested"))

            target = Path(raw) / "Library"
            (source / "通信原理" / "lesson__20260908-100000-abcdef").mkdir(parents=True)
            (source / "通信原理" / "lesson__20260908-100000-abcdef" / "session.json").write_text(
                '{"id":"20260908-100000-abcdef"}', encoding="utf-8"
            )
            (target / "通信原理" / "lesson__20260908-100000-abcdef").mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                settings.migrate_storage_root(str(target))


class DownloadAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_recording_cannot_be_downloaded(self) -> None:
        import app as app_module

        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(
                Path(raw),
                {
                    "id": "20260908-120000-abcdef",
                    "title": "进行中的录音",
                    "category": "未分类",
                    "status": "active",
                    "sample_count": 0,
                },
            )
            with patch.object(app_module.recording_store, "find", return_value=record):
                with self.assertRaises(HTTPException) as raised:
                    await app_module.download_recording(record.id, "audio")

            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("结束并保存", raised.exception.detail)


class IdeaShellParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_current_note_search_markdown(self) -> None:
        from qwen_asr_app.course_file_routes import note_candidates

        payload = {
            "text": (
                "# 光电子器件核心知识与系统架构\n"
                "note_id: 5b386351b8efc03266a64d73f9a2a530\n"
                "time: 2026-09-03T07:19:37.616Z\n\n"
                "## Memos\n"
                "# 正文中的小标题\n"
                "note_id: 这是不合法的编号\n"
            )
        }

        self.assertEqual(
            note_candidates(payload),
            [{
                "note_id": "5b386351b8efc03266a64d73f9a2a530",
                "title": "光电子器件核心知识与系统架构",
                "reason": "闪念贝壳搜索结果",
            }],
        )

    def test_parses_text_folder_listing_and_folder_create_response(self) -> None:
        folders = ideashell_folders(
            {
                "text": "Folders (2):\n- 📡 通信原理 C #通信原理 #课程 [folder_id: 8f44c8fb07b136baa1fe8f8149b47eb9]\n"
                "- 📁 光电子学 [folder_id: 51bcf37841214759871f8a3b405b4e26]"
            }
        )
        self.assertEqual(folders[0]["id"], "8f44c8fb07b136baa1fe8f8149b47eb9")
        self.assertEqual(ideashell_folder_id({"text": "Folder created [folder_id: 51bcf37841214759871f8a3b405b4e26]"}), "51bcf37841214759871f8a3b405b4e26")
        from app import ideashell_note_id
        self.assertEqual(
            ideashell_note_id({"text": "Note created [note_id: 97f7677bd41334f087d8ac21e3a2f78f]"}),
            "97f7677bd41334f087d8ac21e3a2f78f",
        )

    async def test_reuses_existing_decorated_course_folder(self) -> None:
        connection = AsyncMock()
        connection.call_tool = AsyncMock(
            return_value={
                "text": "Folders (1):\n- 💻 微型计算机原理与接口技术 #微机原理 #课程 [folder_id: e8e8ff033ea9dc2f4e59fbe0a948e388]"
            }
        )
        folder_id = await ensure_ideashell_course_folder(connection, "微型计算机原理与接口技术")
        self.assertEqual(folder_id, "e8e8ff033ea9dc2f4e59fbe0a948e388")
        connection.call_tool.assert_awaited_once_with("folder_list", {})

    async def test_reuses_course_folder_with_suffix(self) -> None:
        connection = AsyncMock()
        connection.call_tool = AsyncMock(
            return_value={
                "text": "Folders (1):\n- 📡 通信原理 C #通信原理 #课程 [folder_id: 8f44c8fb07b136baa1fe8f8149b47eb9]"
            }
        )
        folder_id = await ensure_ideashell_course_folder(connection, "通信原理")
        self.assertEqual(folder_id, "8f44c8fb07b136baa1fe8f8149b47eb9")

    async def test_saved_oauth_expiry_is_reduced_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            storage = FileTokenStorage("https://api.ideashell.cn/ideashell/mcp")
            storage.path = Path(raw) / "oauth.json"
            storage.path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": "expired",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                            "refresh_token": "refresh",
                        },
                        "tokens_saved_at": time.time() - 7200,
                    }
                ),
                encoding="utf-8",
            )

            token = await storage.get_tokens()

            self.assertIsNotNone(token)
            self.assertEqual(token.expires_in, 0)

    async def test_connected_session_is_not_restarted_on_every_push(self) -> None:
        connection = IdeaShellConnection("https://api.ideashell.cn/ideashell/mcp")
        connection.connected = True
        connection.storage.tokens_valid_for = lambda: True

        status = await connection.start_connect("http://127.0.0.1:8765/api/ideashell/oauth/callback")

        self.assertTrue(status["connected"])
        self.assertIsNone(connection._connect_task)


class RecordingLibrarySummaryTests(unittest.TestCase):
    def make_record(self, directory: Path, **metadata: object) -> RecordingRecord:
        return RecordingRecord(
            directory,
            {
                "id": "20260907-120000-abcdef",
                "title": "原标题",
                "category": "通信原理",
                "sample_count": 16000,
                **metadata,
            },
        )

    def test_library_summary_prefers_latest_successful_ai_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = self.make_record(
                Path(raw),
                ai_note_summary={
                    "summary": "笔记完成后生成的课程总结",
                    "abstract": "课程核心知识与复习要点",
                },
                ai_summaries=[{"summary": "不应同步的实时总结"}],
            )

            summary, source = record.library_summary()

            self.assertEqual(summary, "课程核心知识与复习要点")
            self.assertEqual(source, "ai")

    def test_library_summary_never_falls_back_to_transcript_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = self.make_record(Path(raw))
            record.segments = [
                {"raw": "原始识别", "corrected": "介绍香农容量公式。"},
                {"raw": "然后分析带宽和信噪比之间的关系。"},
            ]

            summary, source = record.library_summary()

            self.assertEqual(summary, "")
            self.assertEqual(source, "missing")

    def test_chat_text_accepts_array_content_and_reasoning_content(self) -> None:
        self.assertEqual(
            extract_chat_text(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "第一段"},
                                    {"type": "output_text", "text": "第二段"},
                                ]
                            }
                        }
                    ]
                }
            ),
            "第一段第二段",
        )
        self.assertEqual(
            extract_chat_text(
                {
                    "choices": [
                        {"message": {"content": None, "reasoning_content": "最终文本"}}
                    ]
                }
            ),
            "最终文本",
        )


class RecordingTitleGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_polish_prompt_marks_previous_context_read_only(self) -> None:
        mocked = AsyncMock(return_value={"corrected": "A classroom answer."})
        config = {
            "ai_correction": True,
            "ai": {
                "system_prompt": "请修正当前转写。",
                "review_provider": {"context_window": 32768},
            },
            "_transcript_context": "上一段很长的课程内容，不应被复制。",
        }
        with patch("qwen_asr_app.ai.request_ai_json", mocked):
            result = await polish_and_translate("A classroom answer.", "English", config)

        self.assertEqual(result["corrected"], "A classroom answer.")
        request = mocked.await_args.kwargs
        self.assertIn("previous_transcript_context", request["instructions"])
        self.assertIn("只允许修正或翻译 `transcript` 字段", request["instructions"])
        self.assertIn("绝对不要把 `previous_transcript_context`", request["instructions"])
        self.assertEqual(
            request["payload"]["previous_transcript_context"],
            "上一段很长的课程内容，不应被复制。",
        )

    async def test_polish_rejects_context_leak_from_model_output(self) -> None:
        context = "上一段课程内容：" + ("这是不应该被复制到当前段的历史文本。" * 12)
        mocked = AsyncMock(
            return_value={
                "corrected": "当前短句。\n" + context,
            }
        )
        config = {
            "ai_correction": True,
            "ai": {"review_provider": {"context_window": 32768}},
            "_transcript_context": context,
        }
        with patch("qwen_asr_app.ai.request_ai_json", mocked):
            with self.assertRaisesRegex(RuntimeError, "疑似复制了只读上文"):
                await polish_and_translate("当前短句。", "Chinese", config)

    async def test_siliconflow_chat_request_uses_compatible_body(self) -> None:
        captured: dict[str, object] = {}

        async def respond(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": None, "reasoning_content": "摘要正文"}}
                    ]
                },
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        config = {
            "ai": {
                "review_provider": {
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "Qwen/Qwen3.5-35B-A3B",
                    "api_mode": "chat_completions",
                    "context_window": 32768,
                    "extra_params": {
                        "reasoning": {"effort": "none"},
                        "text": {"verbosity": "low"},
                        "store": False,
                    },
                }
            }
        }
        with patch("qwen_asr_app.ai.httpx.AsyncClient", return_value=client):
            result = await request_ai_text(
                instructions="生成摘要",
                payload={"text": "课程内容"},
                config=config,
                mode="review",
                max_output_tokens=180,
            )

        self.assertEqual(result, "摘要正文")
        self.assertEqual(captured["url"], "https://api.siliconflow.cn/v1/chat/completions")
        body = captured["body"]
        self.assertIsInstance(body, dict)
        self.assertNotIn("reasoning", body)
        self.assertNotIn("text", body)
        self.assertNotIn("store", body)
        self.assertFalse(body["enable_thinking"])

    async def test_detailed_summary_survives_abstract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(
                Path(raw),
                {
                    "id": "20260908-100000-abcdef",
                    "title": "香农容量",
                    "category": "通信原理",
                    "status": "completed",
                    "started_at": "2026-09-08T10:00:00+08:00",
                    "sample_count": 16000,
                    "next_segment_id": 2,
                },
            )
            record.segments = [
                {"id": 1, "start": 0, "end": 1, "raw": "讲解香农容量公式。"}
            ]
            with (
                patch(
                    "qwen_asr_app.batch_ai.summarize_transcript",
                    new=AsyncMock(return_value={"summary": "## 公式\n\n$C=B\\log_2(1+S/N)$", "key_points": []}),
                ),
                patch(
                    "qwen_asr_app.batch_ai.summarize_note_abstract",
                    new=AsyncMock(side_effect=RuntimeError("上游 400")),
                ),
            ):
                result = await build_note_summary(
                    record, {"ai": {"review_provider": {"context_window": 32768}}}
                )

            self.assertIn("香农容量", result["summary"])
            self.assertEqual(result["abstract"], "")
            self.assertEqual(result["abstract_error"], "上游 400")
            self.assertTrue(record.ai_summary_path.exists())

    async def test_summary_accepts_plain_markdown_without_json_parsing(self) -> None:
        mocked = AsyncMock(return_value="## 信噪比\n\n公式：$S/N$。\n\n- 定义")
        config = {"ai": {"summary_provider": {"model": "five-minute-model", "context_window": 32768}}}
        with patch("qwen_asr_app.ai.request_ai_text", mocked):
            result = await summarize_transcript("课程内容", "", config, provider_mode="review")

        self.assertIn("## 信噪比", result["summary"])
        self.assertEqual(result["key_points"], [])
        self.assertEqual(mocked.await_args.kwargs["mode"], "review")

    async def test_detailed_summary_and_short_abstract_use_separate_prompts(self) -> None:
        mocked = AsyncMock(side_effect=["## 详细总结", "课程短摘要"])
        config = {
            "_note_summary_system_prompt": "详细总结专用提示词",
            "_note_abstract_system_prompt": "短摘要专用提示词",
            "ai": {"review_provider": {"context_window": 32768}},
        }
        with patch("qwen_asr_app.ai.request_ai_text", mocked):
            await summarize_transcript("课堂内容", "", config, provider_mode="review")
            abstract = await summarize_note_abstract("## 详细总结", config)

        self.assertEqual(abstract, "课程短摘要")
        summary_instructions = mocked.await_args_list[0].kwargs["instructions"]
        abstract_instructions = mocked.await_args_list[1].kwargs["instructions"]
        self.assertIn("详细总结专用提示词", summary_instructions)
        self.assertNotIn("短摘要专用提示词", summary_instructions)
        self.assertIn("短摘要专用提示词", abstract_instructions)
        self.assertNotIn("详细总结专用提示词", abstract_instructions)

    async def test_title_generation_sends_summary_and_custom_prompt(self) -> None:
        mocked = AsyncMock(return_value={"title": "《香农容量与带宽》"})
        with patch("qwen_asr_app.ai.request_ai_json", mocked):
            title = await generate_recording_title(
                "本节讲解香农容量公式以及带宽与信噪比的关系。",
                "使用课程主题作为标题，不超过十个字。",
                {"ai": {}},
                current_title="第三讲",
                category="通信原理",
            )

        self.assertEqual(title, "香农容量与带宽")
        request = mocked.await_args.kwargs
        self.assertIn("使用课程主题作为标题", request["instructions"])
        self.assertIn("香农容量公式", request["payload"]["recording_summary"])

    def test_review_and_summary_use_independent_provider_settings(self) -> None:
        config = {
            "ai": {
                "base_url": "https://realtime.example/v1",
                "model": "realtime-model",
                "context_window": 8192,
                "review_provider": {
                    "base_url": "https://five-minute.example/v1",
                    "model": "five-minute-model",
                    "context_window": 16384,
                },
                "summary_provider": {
                    "base_url": "https://long-context.example/v1",
                    "model": "long-context-model",
                    "context_window": 65536,
                },
            }
        }

        review = provider_for_mode(config["ai"], "review")
        summary = provider_for_mode(config["ai"], "summary")

        self.assertEqual(review["base_url"], "https://five-minute.example/v1")
        self.assertEqual(review["model"], "five-minute-model")
        self.assertEqual(context_window_for(config, "review"), 16384)
        self.assertEqual(summary["base_url"], "https://long-context.example/v1")
        self.assertEqual(summary["model"], "long-context-model")
        self.assertEqual(context_window_for(config, "summary"), 65536)


if __name__ == "__main__":
    unittest.main()
