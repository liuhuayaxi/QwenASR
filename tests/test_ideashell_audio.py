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
from qwen_asr_app.ideashell_attachments import audio_attachment_arguments, audio_attachment_arguments_many, audio_file_metadata
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
    def test_ideashell_title_has_course_date_and_respects_schema_limit(self):
        record = type('Record', (), {'metadata': {
            'title': '半导体光电器件应用与量子跃迁机制',
            'course_time': {'start': '2026-09-02T08:00:00+08:00'},
        }})()
        self.assertEqual(app.ideashell_note_title(record, 30), '0902 半导体光电器件应用与量子跃迁机制')
        record.metadata['title'] = '0902 半导体光电器件应用与量子跃迁机制'
        self.assertEqual(app.ideashell_note_title(record, 30), '0902 半导体光电器件应用与量子跃迁机制')

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

    def test_object_schema_never_silently_drops_the_second_attachment(self):
        schema = {"properties": {"audios": AUDIO_SCHEMA["properties"]["audios"]["items"]}}
        with self.assertRaises(ValueError):
            audio_attachment_arguments_many(schema, [{"name": "a.aac", "data": b"a"}, {"name": "b.aac", "data": b"b"}])

    def test_mcp_task_group_reports_its_underlying_timeout(self):
        error = ExceptionGroup('unhandled errors in a TaskGroup', [ExceptionGroup('nested', [httpx.ReadTimeout('上传超时')])])
        self.assertEqual(app.ideashell_error_text(error), '上传超时')

    def test_pull_extracts_primary_memo_without_retained_split_body(self):
        content = '# 主归档\n\n## AI 总结\n\n完整总结\n\n## 精修录音原文（含时间戳）\n\n完整原文'
        payload = {'text': '# 笔记\nsummary: 短摘要\n\n## Memos\n**Memo 1: 当前归档**\nsummary: 短摘要\n' + content + '\n\n**Memo 2: 旧上半段**\nsummary: 旧摘要\n旧正文'}
        self.assertEqual(app.ideashell_note_content(payload), content)


class IdeaShellPushAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_invisible_old_link_requires_explicit_fresh_note(self):
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(Path(raw), {
                'id': 'trashed-link', 'title': '完整课堂', 'category': '测试课程',
                'status': 'completed', 'sample_count': 96_000,
                'ai_note_summary': {'summary': '完整总结'},
                'ideashell_sync': {'note_id': 'a' * 32, 'note_ids': ['a' * 32]},
            })
            record.segments = [{'id': 1, 'start': 0, 'end': 6, 'raw': '完整原文'}]
            audios = [{'name': '上半段.aac', 'data': b'a', 'bytes': 1, 'sha256': '1' * 64},
                      {'name': '下半段.aac', 'data': b'b', 'bytes': 1, 'sha256': '2' * 64}]
            async def remote(name, arguments):
                return {'text': 'No notes in this folder.'} if name == 'folder_notes' else {'note_id': 'c' * 32}
            connection = SimpleNamespace(server_url='https://mcp.ideashell.cn/mcp',
                list_tools=AsyncMock(return_value={'note_create': AUDIO_SCHEMA, 'note_update': AUDIO_SCHEMA, 'folder_notes': {}}),
                call_tool=AsyncMock(side_effect=remote))
            body = {'preview_digest': compose_push(record)['digest'], 'audio_sha256s': [a['sha256'] for a in audios]}
            with patch.object(app.recording_store, 'find', return_value=record), \
                 patch.object(app, 'ideashell_recording_audio_parts', new=AsyncMock(return_value=audios)), \
                 patch.object(app.ideashell_manager, 'connection', return_value=connection), \
                 patch.object(app, 'ensure_ideashell_course_folder', new=AsyncMock(return_value='b' * 32)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                    rejected = await client.post(f'/api/recordings/{record.id}/ideashell/push', json=body)
                    self.assertEqual(rejected.status_code, 400)
                    fresh = await client.post(f'/api/recordings/{record.id}/ideashell/push', json={**body, 'create_new': True})
            self.assertEqual(fresh.status_code, 200, fresh.text)
            names = [call.args[0] for call in connection.call_tool.await_args_list]
            self.assertNotIn('note_update', names)
            self.assertEqual(names.count('note_create'), 1)
            self.assertEqual(record.metadata['ideashell_sync']['note_ids'], ['c' * 32])
            self.assertEqual(record.metadata['ideashell_sync']['legacy_note_ids'], ['a' * 32])
    async def test_split_preview_contains_one_full_note_and_updates_first_legacy_note(self):
        with tempfile.TemporaryDirectory() as raw:
            record = RecordingRecord(Path(raw), {
                'id': 'legacy-split', 'title': '原完整课堂', 'category': '测试课程',
                'kind': 'upload', 'status': 'completed', 'sample_count': 96_000,
                'ai_note_summary': {'summary': '完整总结'},
                'ideashell_sync': {'note_id': 'a' * 32, 'note_ids': ['a' * 32, 'c' * 32]},
            })
            record.segments = [{'id': 1, 'start': 0, 'end': 3, 'raw': '前半课堂'},
                               {'id': 2, 'start': 3, 'end': 6, 'raw': '后半课堂'}]
            audios = [{'name': '上半段.aac', 'data': b'a', 'bytes': 1, 'sha256': '1' * 64},
                      {'name': '下半段.aac', 'data': b'b', 'bytes': 1, 'sha256': '2' * 64}]
            connection = SimpleNamespace(server_url='https://mcp.ideashell.cn/mcp',
                list_tools=AsyncMock(return_value={'note_update': AUDIO_SCHEMA}), call_tool=AsyncMock(return_value={}))
            with patch.object(app.recording_store, 'find', return_value=record), \
                 patch.object(app, 'ideashell_recording_audio_parts', new=AsyncMock(return_value=audios)), \
                 patch.object(app.ideashell_manager, 'connection', return_value=connection), \
                 patch.object(app, 'ensure_ideashell_course_folder', new=AsyncMock(return_value='b' * 32)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                    preview = (await client.post(f'/api/recordings/{record.id}/ideashell/preview', json={})).json()
                    self.assertEqual(preview['note_count'], 1)
                    self.assertIn('前半课堂', preview['content'])
                    self.assertIn('后半课堂', preview['content'])
                    self.assertEqual(preview['content'].count('完整总结'), 1)
                    hashes = [audio['sha256'] for audio in preview['audio_parts']]
                    stale = await client.post(f'/api/recordings/{record.id}/ideashell/push', json={'preview_digest':preview['digest'], 'audio_sha256s': hashes[::-1]})
                    self.assertEqual(stale.status_code, 400)
                    connection.call_tool.assert_not_awaited()
                    response = await client.post(f'/api/recordings/{record.id}/ideashell/push', json={'preview_digest': preview['digest'], 'audio_sha256s': hashes})
            self.assertEqual(response.status_code, 200, response.text)
            updates = [call.args for call in connection.call_tool.await_args_list if call.args[0] == 'note_update']
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0][1]['note_id'], 'a' * 32)
            self.assertEqual(updates[0][1]['content'], preview['content'])
            self.assertEqual(len(updates[0][1]['audios']), 2)
            self.assertEqual(record.metadata['ideashell_sync']['note_ids'], ['a' * 32])
            self.assertEqual(record.metadata['ideashell_sync']['legacy_note_ids'], ['c' * 32])
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

    async def test_oversized_audio_push_keeps_one_note_with_two_attachments(self):
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
            async def call_tool(name, arguments):
                if name == "note_create": return {"note_id": "a" * 32}
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
            self.assertEqual(payload["note_ids"], ["a" * 32])
            creates = [call.args for call in connection.call_tool.await_args_list if call.args[0] == "note_create"]
            self.assertEqual(len(creates), 1)
            self.assertEqual(
                [base64.b64decode(item["data"]) for item in creates[0][1]["audios"]],
                [b"first", b"second"],
            )

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

