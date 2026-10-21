from __future__ import annotations

from tests import isolation  # Must precede application imports.

import asyncio
import os
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
from qwen_asr_app.ai import (
    AIRequestError,
    normalize_markdown_math,
    polish_timestamped_segments,
    polish_timestamped_segments_resilient,
    request_ai_json,
)
from qwen_asr_app.ai_secrets import AISecretStore
from qwen_asr_app.batch_ai import build_note_summary
from qwen_asr_app.live_session import TranscriptionSession
from qwen_asr_app.storage import RecordingRecord, RecordingStore
from qwen_asr_app.context_budget import DEFAULT_CONTEXT_WINDOW, estimate_tokens
from qwen_asr_app.review import due_summary_windows
from qwen_asr_app.terminology import (
    MAX_AI_GLOSSARY_TOKENS,
    asr_params_with_terminology,
    ai_terminology_context,
    normalize_terminology,
)


class RetryStormTests(unittest.TestCase):
    def test_failed_live_window_is_not_automatically_enqueued_again(self) -> None:
        segments = [
            {"id": 1, "start": 0, "end": 600, "raw": "第一段"},
            {"id": 2, "start": 600, "end": 1200, "raw": "第二段"},
        ]
        saved = [{"id": "0-600", "start": 0, "end": 600, "ai_error": "DNS failed"}]
        due = due_summary_windows(segments, saved, 600)
        self.assertEqual([(start, end) for start, end, _ in due], [(600, 1200)])

    def test_verification_recovery_respects_explicit_refinement_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            record = RecordingRecord(Path(directory), {
                'id': 'failed-summary', 'status': 'completed', 'title': '失败总结',
                'category': '测试课程',
                'summary_job': {
                    'status': 'failed',
                    'error': 'AGY 文本会话尝试调用未授权工具：read_file',
                    'error_kind': 'material_tool_violation',
                },
                'ai_jobs': {'refinement': {'status': 'cancelled'}},
            })
            record.save_metadata()
            with patch.object(app.recording_store, 'find', return_value=record), \
                 patch('qwen_asr_app.transcription_assist.routes.active_candidates', return_value=[{'candidate_id': 'x'}]), \
                 patch.object(app, 'ai_task_action', new=AsyncMock()) as action:
                asyncio.run(app.queue_correction_verification(record.id))
            action.assert_not_awaited()

    def test_task_center_deduplicates_historical_attempts(self) -> None:
        record = SimpleNamespace(
            id="lesson-1",
            segments=[{"id": 1, "start": 0, "end": 600, "raw": "内容", "corrected": "内容"}],
            metadata={
                "title": "通信原理",
                "category": "通信原理",
                "status": "completed",
                "summary_job": {"status": "completed"},
                "ai_jobs": {},
                "ai_reviews": [],
                "ai_summaries": [
                    {"id": "old-1", "start": 0, "end": 600, "ai_error": "DNS failed"},
                    {"id": "old-2", "start": 0, "end": 600, "ai_error": "DNS failed again"},
                ],
            },
        )
        with patch.object(app.recording_store, "iter_records", return_value=iter([record])):
            tasks = app.compact_ai_tasks()
        live = [item for item in tasks if item["type"] == "live_summary"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["attempts"], 2)


class CumulativeChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_windows_wait_for_manual_resume_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            record = RecordingRecord(directory, {
                "id": "live-chain", "title": "课堂", "category": "未分类",
                "kind": "microphone", "status": "active", "sample_count": 0,
                "next_segment_id": 3, "sample_rate": 16000,
                "audio_filename": "recording.aac", "working_audio_filename": ".recording-recovery.wav",
                "ai_summaries": [{"start": 0, "end": 600, "ai_error": "DNS failed"}],
            })
            record._create_empty_wav()
            record.segments = [
                {"id": 1, "start": 0, "end": 600, "raw": "第一窗"},
                {"id": 2, "start": 600, "end": 1200, "raw": "第二窗"},
            ]
            record.save_all()
            store = RecordingStore.__new__(RecordingStore)
            store.active, store.paths = record, {record.id: directory}
            session = TranscriptionSession(
                SimpleNamespace(send_json=AsyncMock()), SimpleNamespace(), store
            )
            with patch("qwen_asr_app.live_session.summarize_transcript", new=AsyncMock()) as summarize:
                await session.run_periodic_summaries(
                    record,
                    {"summary_interval_minutes": 10, "ai": {"review_provider": {"context_window": 32768}}},
                )
            summarize.assert_not_awaited()


class SummaryFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_full_note_retries_with_smaller_complete_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            record = RecordingRecord(directory, {
                "id": "summary-fallback", "title": "长课", "category": "通信原理",
                "kind": "upload", "status": "completed", "sample_count": 0,
                "next_segment_id": 4, "sample_rate": 16000, "audio_filename": "recording.aac",
                "started_at": "2026-09-09T10:00:00+08:00",
            })
            record.segments = [
                {"id": value, "start": value * 60, "end": (value + 1) * 60,
                 "raw": f"第{value}段" + "公式推导。" * 1000}
                for value in range(1, 4)
            ]
            record.save_all()
            seen: list[bool] = []

            async def summarize(text, previous, options, **kwargs):
                seen.append(bool(options.get("_full_note")))
                if len(seen) == 1:
                    raise AIRequestError("输出截断", kind="output_truncated")
                return {"summary": "## 知识点速览\n\n完整\n\n## 公式速览\n\n完整\n\n## 按时间展开\n\n完整"}

            with (
                patch("qwen_asr_app.batch_ai.summarize_transcript", side_effect=summarize),
                patch("qwen_asr_app.batch_ai.summarize_note_abstract", new=AsyncMock(return_value="短摘要")),
            ):
                result = await build_note_summary(
                    record, {"ai": {"summary_provider": {"context_window": 131072}}}
                )
            self.assertTrue(seen[0])
            self.assertTrue(all(not value for value in seen[1:]))
            self.assertGreater(result["parts"], 1)
            self.assertEqual(result["input_mode"], "chunked")


class TimestampMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_translation_keeps_one_translation_per_original_id(self):
        segments=[{'id':1,'raw':'甲'},{'id':2,'raw':'乙'}]
        response={'blocks':[{'segment_ids':[1],'corrected':'甲','translation':'A'},
                            {'segment_ids':[2],'corrected':'乙','translation':'B'}]}
        with patch('qwen_asr_app.ai.request_ai_json',new=AsyncMock(return_value=response)) as request:
            result=await polish_timestamped_segments(segments,'Chinese',{'translation':'en','ai':{}},mode='global')
        self.assertEqual([b['translation'] for b in result],['A','B'])
        self.assertTrue(all(b['translation_target']=='English' for b in result))
        self.assertIn('translation',request.await_args.kwargs['instructions'])
        response['blocks'][1].pop('translation')
        with patch('qwen_asr_app.ai.request_ai_json',new=AsyncMock(return_value=response)):
            with self.assertRaisesRegex(AIRequestError,'译文'):
                await polish_timestamped_segments(segments,'Chinese',{'translation':'en','ai':{}},mode='global')

    async def test_adjacent_paragraphs_keep_exact_source_ids_in_one_request(self) -> None:
        segments = [
            {"id": 11, "raw": "奈奎斯特", "start": 1, "end": 2},
            {"id": 12, "raw": "准则", "start": 2, "end": 3},
            {"id": 13, "raw": "无码间串扰", "start": 3, "end": 4},
        ]
        response = {"blocks": [
            {"segment_ids": [11], "corrected": "奈奎斯特"},
            {"segment_ids": [12], "corrected": "准则"},
            {"segment_ids": [13], "corrected": "无码间串扰"},
        ]}
        config = {"ai": {"review_provider": {"context_window": 32768}}}
        with patch("qwen_asr_app.ai.request_ai_json", new=AsyncMock(return_value=response)):
            result = await polish_timestamped_segments(segments, "zh", config)
        self.assertEqual([item["segment_ids"] for item in result], [[11], [12], [13]])

    async def test_missing_source_id_is_rejected(self) -> None:
        segments = [{"id": 1, "raw": "甲"}, {"id": 2, "raw": "乙"}]
        response = {"blocks": [{"segment_ids": [1], "corrected": "甲"}]}
        config = {"ai": {"review_provider": {"context_window": 32768}}}
        with patch("qwen_asr_app.ai.request_ai_json", new=AsyncMock(return_value=response)):
            with self.assertRaisesRegex(AIRequestError, "遗漏或重复"):
                await polish_timestamped_segments(segments, "zh", config)

    async def test_invalid_large_mapping_is_bisected_without_losing_ids(self) -> None:
        segments = [{"id": 1, "raw": "甲"}, {"id": 2, "raw": "乙"}]

        async def refine(items, *_args, **_kwargs):
            if len(items) > 1:
                raise AIRequestError("映射错误", kind="invalid_segment_mapping")
            return [{"segment_ids": [items[0]["id"]], "corrected": items[0]["raw"] + "。"}]

        with patch("qwen_asr_app.ai.polish_timestamped_segments", side_effect=refine):
            result = await polish_timestamped_segments_resilient(
                segments, "zh", {"ai_correction": True, "ai": {}}, mode="correction"
            )
        self.assertEqual([item["segment_ids"] for item in result], [[1], [2]])

    async def test_one_oversized_segment_is_split_and_rejoined(self) -> None:
        segment = {"id": 7, "raw": ("第一句内容很长。第二句继续解释。" * 20)}

        async def direct(text, *_args, **_kwargs):
            if len(text) > 140:
                raise AIRequestError("片段长度太长", kind="input_too_long")
            return {"corrected": text}

        with (
            patch("qwen_asr_app.ai.polish_timestamped_segments", new=AsyncMock(
                side_effect=AIRequestError("片段长度太长", kind="input_too_long")
            )),
            patch("qwen_asr_app.ai.polish_and_translate", side_effect=direct) as refine,
        ):
            result = await polish_timestamped_segments_resilient(
                [segment], "zh", {"ai_correction": True, "ai": {}}, mode="correction"
            )
        self.assertEqual(result[0]["segment_ids"], [7])
        self.assertGreater(refine.await_count, 1)
        self.assertIn("第一句内容很长", result[0]["corrected"])


class StructuredOutputRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_json_is_regenerated_once(self) -> None:
        with patch(
            "qwen_asr_app.ai.request_ai_text",
            new=AsyncMock(side_effect=['{"blocks":[{"segment_ids":[1] "corrected":"甲"}]}',
                                       '{"blocks":[{"segment_ids":[1],"corrected":"甲"}]}']),
        ) as request:
            value = await request_ai_json(
                instructions="返回 JSON", payload={"segments": [1]}, config={"ai": {}},
                mode="correction", max_output_tokens=600,
            )
        self.assertEqual(value["blocks"][0]["corrected"], "甲")
        self.assertEqual(request.await_count, 2)


class MarkdownMathTests(unittest.TestCase):
    def test_greek_symbols_do_not_swallow_adjacent_variables(self) -> None:
        for source, expected in (
            ("V = (2πa / λ) √(n₁² - n₂²)", r"V = (2\pi a / \lambda) \sqrt{n_1^2 - n_2^2}"),
            ("V = (2πa / λ) × NA", r"V = (2\pi a / \lambda) \times NA"),
            ("Δn ≈ θx + λf", r"\Delta n \approx \theta x + \lambda f"),
        ):
            for wrapper in ("`", "**"):
                with self.subTest(source=source, wrapper=wrapper):
                    self.assertEqual(normalize_markdown_math(f"{wrapper}{source}{wrapper}"), f"${expected}$")

    def test_formula_code_spans_become_math_but_normal_code_stays_code(self) -> None:
        markdown = "计算 `sin θ_c = n2 / n1 ≈ 0.9865`，命令 `git status`。"
        normalized = normalize_markdown_math(markdown)
        self.assertIn(r"$\sin \theta_c = n_2 / n_1 \approx 0.9865$", normalized)
        self.assertIn("`git status`", normalized)

    def test_bold_equation_becomes_standard_inline_latex(self) -> None:
        normalized = normalize_markdown_math("根据 **NA = √(n₁² - n₂²)** 计算。")
        self.assertIn(r"$NA = \sqrt{n_1^2 - n_2^2}$", normalized)


class AISecretStoreTests(unittest.TestCase):
    def test_provider_keys_persist_with_user_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / ".qwenasr" / "ai-provider-secrets.json"
            store = AISecretStore(path)
            status = store.update({"sentence": "secret-a", "review": "secret-b"}, {"sentence": "https://a.invalid/v1", "review": "https://b.invalid/v1"})
            self.assertTrue(status["sentence"])
            self.assertEqual(store.get("review", "https://b.invalid/v1"), "secret-b")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            store.clear()
            self.assertFalse(path.exists())


class TerminologyBudgetTests(unittest.TestCase):
    def test_course_seed_and_asr_hotword_cap(self) -> None:
        seeded = normalize_terminology(None, category="通信原理", seed_if_missing=True)
        self.assertIn("奈奎斯特准则", seeded)
        params = asr_params_with_terminology({"hotwords": ["已有词"]}, [f"术语{i}" for i in range(500)])
        self.assertEqual(len(params["hotwords"]), 100)
        self.assertEqual(params["hotwords"][0], "已有词")

    def test_ai_glossary_and_default_context_are_bounded(self) -> None:
        glossary = ai_terminology_context(
            [f"专业术语{i} | 别名：词{i}" for i in range(500)],
            context_window=131072,
            source_text="这里提到专业术语499",
        )
        self.assertLessEqual(estimate_tokens(glossary), MAX_AI_GLOSSARY_TOKENS)
        self.assertEqual(DEFAULT_CONTEXT_WINDOW, 131072)


class SegmentRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_failed_segment_can_retry_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(Path(raw), {
                "id": "single-retry", "title": "光电子器件", "category": "光电子学",
                "kind": "microphone", "status": "interrupted", "sample_count": 0,
                "next_segment_id": 2, "sample_rate": 16000,
                "audio_filename": "recording.aac", "working_audio_filename": ".recording-recovery.wav",
            })
            record.segments = [{
                "id": 1, "start": 0, "end": 5, "raw": "Snells low",
                "language": "English", "ai_error": "DNS failed", "ai_pending": False,
                "ai_requested": {"correction": True, "translation": "off"},
            }]
            record.save_all()
            config = {"ai_correction": True, "translation": "off", "ai": {}}
            with (
                patch.object(app.recording_store, "find", return_value=record),
                patch.object(app, "polish_and_translate", new=AsyncMock(
                    return_value={"corrected": "Snell's law"}
                )),
            ):
                result = await app.retry_segment_ai(record.id, 1, {"config": config})
            self.assertTrue(result["success"])
            self.assertEqual(record.segments[0]["corrected"], "Snell's law")
            self.assertNotIn("ai_error", record.segments[0])
            self.assertFalse(record.segments[0]["ai_pending"])


class AutomaticRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_aux_task_waits_and_recovers_from_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(Path(raw), {
                "id": "auto-retry", "title": "课堂", "category": "未分类",
                "kind": "upload", "status": "completed", "sample_count": 0,
                "next_segment_id": 1, "sample_rate": 16000,
                "audio_filename": "recording.aac",
            })
            record.save_all()
            calls = 0

            async def runner() -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise AIRequestError("status=429 rate limit", kind="rate_limit")

            with patch.object(app, "_automatic_retry_delay", return_value=0):
                self.assertTrue(app._start_aux_ai_job(record, "refinement", runner))
                await app.background_ai_tasks[f"refinement:{record.id}"]
            self.assertEqual(calls, 2)
            self.assertEqual(record.metadata["ai_jobs"]["refinement"]["status"], "completed")
            self.assertEqual(record.metadata["ai_jobs"]["refinement"]["auto_retry_count"], 1)

    async def test_bulk_pause_marks_running_task_paused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(Path(raw), {
                "id": "bulk-pause", "title": "课堂", "category": "未分类",
                "kind": "upload", "status": "completed", "sample_count": 0,
                "next_segment_id": 1, "sample_rate": 16000,
                "audio_filename": "recording.aac",
            })
            record.save_all()

            async def runner() -> None:
                await asyncio.Event().wait()

            self.assertTrue(app._start_aux_ai_job(record, "refinement", runner))
            await asyncio.sleep(0)
            with patch.object(app.recording_store, "find", return_value=record):
                result = await app.bulk_ai_task_action({
                    "action": "pause",
                    "items": [{"recording_id": record.id, "type": "refinement"}],
                })
            await asyncio.sleep(0)
            self.assertEqual(result["accepted"], 1)
            self.assertEqual(record.metadata["ai_jobs"]["refinement"]["status"], "paused")


if __name__ == "__main__":
    unittest.main()
