from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from tests import isolation  # noqa: F401  # Must precede application imports.

import app
from qwen_asr_app.ideashell_attachments import audio_attachment_arguments, audio_file_metadata
from qwen_asr_app.note_content import compose_push, compose_push_part
from qwen_asr_app.storage import RecordingRecord


AUDIO_SCHEMA = {
    "type": "object",
    "properties": {
        "audios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "data": {"type": "string"}},
                "required": ["name", "data"],
            },
        }
    },
}


class IdeaShellAudioAttachmentTests(unittest.TestCase):
    def test_schema_arguments_are_base64_encoded(self):
        result = audio_attachment_arguments(AUDIO_SCHEMA, "课堂.aac", b"abc")
        self.assertEqual(result["audios"][0], {"name": "课堂.aac", "data": base64.b64encode(b"abc").decode()})

    def test_metadata_reports_digest_without_exposing_bytes_in_preview(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "recording.aac"
            path.write_bytes(b"abc")
            metadata = audio_file_metadata(path, filename="课程.aac")
        self.assertEqual(metadata["name"], "课程.aac")
        self.assertEqual(metadata["bytes"], 3)
        self.assertEqual(metadata["data"], b"abc")
        self.assertEqual(len(metadata["sha256"]), 64)


class IdeaShellPushAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_attaches_audio_by_default(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            record = RecordingRecord(
                folder,
                {
                    "id": "upload-audio-test",
                    "title": "通信课堂",
                    "category": "通信原理",
                    "kind": "upload",
                    "status": "completed",
                    "sample_count": 48_000,
                    "audio_filename": "recording.aac",
                    "started_at": "2026-10-10T10:00:00+08:00",
                    "ai_note_summary": {"summary": "课堂总结", "abstract": "摘要"},
                },
            )
            record.audio_path.write_bytes(b"audio-bytes")
            record.segments = [{"id": 1, "start": 0, "end": 3, "raw": "原文"}]
            digest = compose_push(record, "summary")["digest"]
            note_id = "a" * 32
            connection = SimpleNamespace(
                server_url="https://mcp.ideashell.cn/mcp",
                list_tools=AsyncMock(return_value={"note_create": AUDIO_SCHEMA}),
                call_tool=AsyncMock(return_value={"note_id": note_id}),
            )
            with patch.object(app.recording_store, "find", return_value=record), \
                    patch.object(app.ideashell_manager, "connection", return_value=connection), \
                    patch.object(app, "ensure_ideashell_course_folder", new=AsyncMock(return_value="b" * 32)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app.app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        f"/api/recordings/{record.id}/ideashell/push",
                        json={"content_mode": "summary", "preview_digest": digest},
                    )
            self.assertEqual(response.status_code, 200, response.text)
            arguments = next(call.args[1] for call in connection.call_tool.await_args_list if call.args[0] == "note_create")
            self.assertEqual(arguments["audios"][0]["name"], "通信课堂.aac")
            self.assertEqual(base64.b64decode(arguments["audios"][0]["data"]), b"audio-bytes")
            self.assertTrue(response.json()["audio_attached"])

    async def test_oversized_audio_push_creates_two_notes_with_two_attachments(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            record = RecordingRecord(folder, {
                "id": "split-audio-test", "title": "课堂分段", "category": "测试课程",
                "kind": "upload", "status": "completed", "sample_count": 96_000,
                "audio_filename": "recording.aac", "started_at": "2026-10-10T10:00:00+08:00",
                "ai_note_summary": {"summary": "完整总结", "abstract": "摘要"},
            })
            record.segments = [
                {"id": 1, "start": 0, "end": 3, "raw": "上半段"},
                {"id": 2, "start": 3, "end": 6, "raw": "下半段"},
            ]
            archives = [compose_push_part(record, "combined", 0, 3, 1), compose_push_part(record, "combined", 3, 6, 2)]
            audios = [
                {"name": "课堂分段（第1段）.aac", "data": b"first", "bytes": 5, "sha256": "1" * 64},
                {"name": "课堂分段（第2段）.aac", "data": b"second", "bytes": 6, "sha256": "2" * 64},
            ]
            note_ids = iter(["a" * 32, "c" * 32])
            async def call_tool(name, arguments):
                if name == "note_create": return {"note_id": next(note_ids)}
                return {}
            connection = SimpleNamespace(
                server_url="https://mcp.ideashell.cn/mcp",
                list_tools=AsyncMock(return_value={"note_create": AUDIO_SCHEMA}),
                call_tool=AsyncMock(side_effect=call_tool),
            )
            with patch.object(app.recording_store, "find", return_value=record), \
                    patch.object(app, "ideashell_recording_audio_parts", new=AsyncMock(return_value=audios)), \
                    patch.object(app.ideashell_manager, "connection", return_value=connection), \
                    patch.object(app, "ensure_ideashell_course_folder", new=AsyncMock(return_value="b" * 32)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
                    response = await client.post(
                        f"/api/recordings/{record.id}/ideashell/push",
                        json={"content_mode": "combined", "split_parts": [
                            {"digest": archives[0]["digest"], "audio_sha256": audios[0]["sha256"]},
                            {"digest": archives[1]["digest"], "audio_sha256": audios[1]["sha256"]},
                        ]},
                    )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["parts"], 2)
            self.assertEqual(len(payload["note_ids"]), 2)
            creates = [call.args for call in connection.call_tool.await_args_list if call.args[0] == "note_create"]
            self.assertEqual(len(creates), 2)
            self.assertEqual(base64.b64decode(creates[0][1]["audios"][0]["data"]), b"first")
            self.assertEqual(base64.b64decode(creates[1][1]["audios"][0]["data"]), b"second")

    async def test_text_push_survives_missing_audio(self):
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(Path(raw), {
                "id": "text-without-audio", "title": "只有文字", "category": "测试课程",
                "kind": "upload", "status": "completed", "sample_count": 48_000,
                "started_at": "2026-10-10T10:00:00+08:00", "ai_note_summary": {"summary": "总结"},
            })
            record.segments = [{"id": 1, "start": 0, "end": 3, "raw": "文字"}]
            digest = compose_push(record, "summary")["digest"]
            note_id = "d" * 32
            connection = SimpleNamespace(
                server_url="https://mcp.ideashell.cn/mcp",
                list_tools=AsyncMock(return_value={"note_create": AUDIO_SCHEMA}),
                call_tool=AsyncMock(return_value={"note_id": note_id}),
            )
            with patch.object(app.recording_store, "find", return_value=record), \
                    patch.object(app, "ideashell_recording_audio_parts", new=AsyncMock(side_effect=ValueError("音频不存在"))), \
                    patch.object(app.ideashell_manager, "connection", return_value=connection), \
                    patch.object(app, "ensure_ideashell_course_folder", new=AsyncMock(return_value="b" * 32)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
                    response = await client.post(f"/api/recordings/{record.id}/ideashell/push", json={"content_mode": "summary", "preview_digest": digest})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["audio_attached"])
            arguments = connection.call_tool.await_args.args[1]
            self.assertNotIn("audios", arguments)

