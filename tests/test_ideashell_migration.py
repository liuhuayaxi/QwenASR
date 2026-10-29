from tests import isolation  # Must precede application imports.

import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import httpx2
from fastapi import FastAPI

from qwen_asr_app.ideashell_sync import (
    FileTokenStorage, IdeaShellConnection, IdeaShellManager,
    IDEASHELL_SERVERS, LEGACY_CHINA_SERVER, normalize_server_url, same_server,
)
from qwen_asr_app.course_file_routes import CourseFileAPI, already_synced, attachment_arguments

NEW = IDEASHELL_SERVERS["china"]
GLOBAL = IDEASHELL_SERVERS["global"]
DOCUMENT_SCHEMA = {
    "type": "object", "required": ["note_id"], "additionalProperties": False,
    "properties": {
        "note_id": {"type": "string"}, "content": {"type": "string"},
        "documents": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "required": ["data", "name"], "additionalProperties": False,
            "properties": {"data": {"type": "string"}, "name": {"type": "string"}},
        }},
    },
}


class MigrationTests(unittest.TestCase):
    def test_legacy_links_keep_region_and_connection_identity(self):
        self.assertEqual(normalize_server_url(" " + LEGACY_CHINA_SERVER + "/ "), NEW)
        manager = IdeaShellManager()
        self.assertIs(manager.connection(LEGACY_CHINA_SERVER), manager.connection(NEW))
        self.assertEqual(manager.active_url, NEW)
        self.assertEqual(FileTokenStorage(NEW).path, FileTokenStorage(LEGACY_CHINA_SERVER).path)
        self.assertNotEqual(FileTokenStorage(NEW).path, FileTokenStorage(GLOBAL).path)
        self.assertFalse(same_server(GLOBAL, NEW))
        self.assertFalse(same_server("https://untrusted.example/mcp", NEW))
        self.assertFalse(same_server(None, None))

    def test_new_documents_payload_appends_without_body_replacement(self):
        data = b"a,b\n1,2"
        args = attachment_arguments(DOCUMENT_SCHEMA, "table.csv", data)
        self.assertEqual(args, {"documents": [{"name": "table.csv", "data": base64.b64encode(data).decode()}]})
        self.assertNotIn("content", args)
        incompatible = {**DOCUMENT_SCHEMA, "required": ["note_id", "content"]}
        with self.assertRaisesRegex(ValueError, "替换笔记正文"):
            attachment_arguments(incompatible, "table.csv", data)

    def test_receipt_alias_does_not_hide_different_uploads(self):
        target = {"server_url": NEW, "note_id": "a" * 32, "sha256": "hash"}
        receipt = {**target, "server_url": LEGACY_CHINA_SERVER, "status": "synced"}
        self.assertTrue(already_synced([receipt], target))
        for change in ({"server_url": GLOBAL}, {"note_id": "b" * 32}, {"sha256": "other"}, {"status": "unknown"}):
            self.assertFalse(already_synced([{**receipt, **change}], target))


class OAuthMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_legacy_grant_refreshes_at_original_issuer_for_new_transport(self):
        with tempfile.TemporaryDirectory() as raw:
            connection = IdeaShellConnection(NEW)
            connection.storage.path = Path(raw) / "oauth.json"
            connection.storage.path.write_text(json.dumps({
                "client_info": {"client_id": "test-client", "issuer": "https://api.ideashell.cn",
                                "redirect_uris": [connection._callback_url], "token_endpoint_auth_method": "none"},
                "tokens": {"access_token": "expired", "refresh_token": "refresh", "token_type": "Bearer", "expires_in": 10},
                "tokens_saved_at": time.time() - 100,
                "discovery": {
                    "server_url": LEGACY_CHINA_SERVER, "auth_server_url": "https://api.ideashell.cn",
                    "oauth_metadata": {"issuer": "https://api.ideashell.cn",
                        "authorization_endpoint": "https://api.ideashell.cn/oauth/authorize",
                        "token_endpoint": "https://api.ideashell.cn/oauth/token",
                        "response_types_supported": ["code"], "token_endpoint_auth_methods_supported": ["none"]},
                    "protected_resource_metadata": {"resource": LEGACY_CHINA_SERVER, "authorization_servers": ["https://api.ideashell.cn"]},
                },
            }))
            calls = []

            def respond(request):
                calls.append(request)
                if str(request.url) == "https://api.ideashell.cn/oauth/token":
                    return httpx2.Response(200, json={"access_token": "renewed", "refresh_token": "rotated", "token_type": "Bearer", "expires_in": 3600})
                self.assertEqual(str(request.url), NEW)
                self.assertEqual(request.headers["authorization"], "Bearer renewed")
                return httpx2.Response(200, json={"ok": True})

            async with httpx2.AsyncClient(auth=connection._build_auth(), transport=httpx2.MockTransport(respond)) as client:
                response = await client.post(NEW, json={"method": "initialize"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(calls), 2)
            self.assertIn(b"grant_type=refresh_token", calls[0].content)
            self.assertIn(b"resource=https%3A%2F%2Fapi.ideashell.cn%2Fideashell%2Fmcp", calls[0].content)
            saved = connection.storage._read()
            self.assertEqual(saved["tokens"]["refresh_token"], "rotated")
            self.assertEqual(saved["client_info"]["issuer"], "https://api.ideashell.cn")
            self.assertEqual(connection.storage.path.stat().st_mode & 0o777, 0o600)

    async def test_new_or_different_issuer_grants_use_new_discovery(self):
        with tempfile.TemporaryDirectory() as raw:
            storage = FileTokenStorage(NEW)
            storage.path = Path(raw) / "oauth.json"
            self.assertEqual(storage.oauth_resource_url(NEW), NEW)
            storage.path.write_text(json.dumps({
                "client_info": {"issuer": "https://mcp.ideashell.cn"},
                "discovery": {"server_url": LEGACY_CHINA_SERVER, "auth_server_url": "https://api.ideashell.cn"},
            }))
            self.assertEqual(storage.oauth_resource_url(NEW), NEW)


class AttachmentRouteMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_links_remain_candidates_and_old_receipts_prevent_reupload(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "lecture.pdf"
            path.write_bytes(b"course attachment")
            ident = "a" * 32
            item = {"sync": [{"server_url": LEGACY_CHINA_SERVER, "note_id": ident,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "status": "synced"}]}
            store = Mock()
            store.get.return_value = (item, Path(raw))
            store.file.return_value = (item, path)
            connection = SimpleNamespace(server_url=NEW, list_tools=AsyncMock(return_value={"note_update": DOCUMENT_SCHEMA}),
                                         call_tool=AsyncMock(return_value={"title": "目标笔记"}))
            manager = SimpleNamespace(connection=Mock(return_value=connection))
            recordings = SimpleNamespace(list_records=lambda: [SimpleNamespace(metadata={
                "category": "课程", "title": "录音笔记", "ideashell_sync": {"server_url": LEGACY_CHINA_SERVER, "note_id": "b" * 32},
            })])
            with patch("qwen_asr_app.course_file_routes.CourseFiles", return_value=store):
                api = CourseFileAPI(None, recordings, manager, None)
            app = FastAPI()
            app.include_router(api.router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
                                         headers={"X-QwenASR-Client": "course-files"}) as client:
                response = await client.post("/api/course-files/asset/matches", json={"category": "课程", "server_url": NEW})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual({n["note_id"] for n in response.json()["candidates"]}, {ident, "b" * 32})
                response = await client.post("/api/course-files/asset/preview", json={"category": "课程", "server_url": NEW, "note_id": ident})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["duplicate"])
                response = await client.post("/api/course-files/asset/sync", json={"token": response.json()["token"]})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["duplicate"])
            connection.call_tool.assert_awaited_once_with("note_detail", {"note_id": ident, "scope": "brief"})
            store.save.assert_not_called()


class StaleGrantTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_mismatch_sets_old_grant_aside_and_starts_fresh_auth(self):
        from mcp.client.auth.exceptions import OAuthFlowError
        from qwen_asr_app import ideashell_sync
        with tempfile.TemporaryDirectory() as tmp, patch.object(ideashell_sync, "PRIVATE_ROOT", Path(tmp)):
            connection = IdeaShellConnection(NEW)
            connection.storage.path.write_text(json.dumps({"tokens": {"access_token": "old"}}))
            mismatch = ExceptionGroup("unhandled errors in a TaskGroup", [OAuthFlowError(
                f"Protected resource {NEW} does not match expected {LEGACY_CHINA_SERVER}")])
            calls = []

            async def session():
                calls.append(connection.storage.path.exists())
                if len(calls) == 1:
                    raise mismatch
                connection.authorization_url = "https://mcp.ideashell.cn/authorize?x=1"
                raise RuntimeError("waiting for user")

            connection._client_session = session
            await connection._probe_locked()
            self.assertEqual(calls, [True, False], "second attempt runs without the stale grant")
            backups = list(Path(tmp).glob("*.stale-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertIn("old", backups[0].read_text())

    def test_readable_error_flattens_task_groups(self):
        from mcp.client.auth.exceptions import OAuthFlowError
        from qwen_asr_app.ideashell_sync import readable_error
        group = ExceptionGroup("unhandled errors in a TaskGroup", [OAuthFlowError("Protected resource a does not match expected b")])
        self.assertIn("重新授权", readable_error(group))
        self.assertEqual(readable_error(ExceptionGroup("g", [RuntimeError("boom")])), "boom")
