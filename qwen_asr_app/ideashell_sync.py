from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

IDEASHELL_SERVERS = {
    "china": "https://api.ideashell.cn/ideashell/mcp",
    "global": "https://api.ideashell.com/ideashell/mcp",
}


def normalize_server_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url not in IDEASHELL_SERVERS.values():
        raise ValueError("IdeaShell 服务地址必须选择中国大陆或全球区域")
    return url


class FileTokenStorage:
    """Persist MCP OAuth tokens and dynamic client registration with user-only access."""

    def __init__(self, server_url: str) -> None:
        host = urlparse(server_url).hostname or "ideashell"
        self.path = Path.home() / ".qwenasr" / f"{host}-oauth.json"

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def _token_remaining(self, payload: dict[str, Any]) -> float | None:
        raw = payload.get("tokens")
        if not isinstance(raw, dict) or raw.get("expires_in") is None:
            return None
        saved_at = payload.get("tokens_saved_at")
        if not isinstance(saved_at, (int, float)):
            try:
                saved_at = self.path.stat().st_mtime
            except OSError:
                return None
        try:
            elapsed = max(0.0, time.time() - float(saved_at))
            return float(raw["expires_in"]) - elapsed
        except (TypeError, ValueError, OverflowError):
            return None

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        payload = self._read()
        raw = payload.get("tokens")
        if not isinstance(raw, dict):
            return None

        # MCP stores only a relative ``expires_in`` value.  Once that value is
        # persisted to disk it must be reduced by the time spent offline;
        # otherwise every server restart treats an expired access token as new
        # for another hour and the first MCP request receives a 401 instead of
        # refreshing it.  Older token files have no timestamp, so their file
        # mtime is a safe fallback.
        adjusted = dict(raw)
        remaining = self._token_remaining(payload)
        if remaining is not None:
            adjusted["expires_in"] = max(0, int(remaining))
        return OAuthToken.model_validate(adjusted)

    async def set_tokens(self, tokens: Any) -> None:
        payload = self._read()
        payload["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        payload["tokens_saved_at"] = time.time()
        self._write(payload)

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if isinstance(raw, dict) else None

    async def set_client_info(self, client_info: Any) -> None:
        payload = self._read()
        payload["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(payload)

    def has_saved_tokens(self) -> bool:
        return isinstance(self._read().get("tokens"), dict)

    def tokens_valid_for(self, minimum_seconds: float = 30.0) -> bool:
        payload = self._read()
        raw = payload.get("tokens")
        if not isinstance(raw, dict) or not str(raw.get("access_token") or "").strip():
            return False
        remaining = self._token_remaining(payload)
        return remaining is None or remaining > minimum_seconds


class IdeaShellConnection:
    def __init__(self, server_url: str) -> None:
        self.server_url = normalize_server_url(server_url)
        self.storage = FileTokenStorage(server_url)
        self.authorization_url = ""
        self.error = ""
        self.connected = False
        self._callback_url = ""
        self._redirect_ready = asyncio.Event()
        self._callback_queue: asyncio.Queue[dict[str, str | None]] = asyncio.Queue(maxsize=1)
        self._connect_task: asyncio.Task[Any] | None = None
        self._call_lock = asyncio.Lock()

    async def _redirect_handler(self, auth_url: str) -> None:
        self.authorization_url = str(auth_url)
        self._redirect_ready.set()

    async def _callback_handler(self) -> Any:
        from mcp.client.auth import AuthorizationCodeResult

        payload = await asyncio.wait_for(self._callback_queue.get(), timeout=300)
        return AuthorizationCodeResult(
            code=str(payload.get("code") or ""),
            state=payload.get("state"),
            iss=payload.get("iss"),
        )

    async def deliver_callback(
        self, code: str, state: str | None, iss: str | None
    ) -> None:
        if self._callback_queue.full():
            self._callback_queue.get_nowait()
        await self._callback_queue.put({"code": code, "state": state, "iss": iss})

    def _build_auth(self) -> Any:
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        from pydantic import AnyUrl

        return OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=OAuthClientMetadata(
                client_name="QwenASR IdeaShell Sync",
                redirect_uris=[AnyUrl(self._callback_url)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="user",
            ),
            storage=self.storage,
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
        )

    async def _client_session(self) -> Any:
        # Import lazily so the transcription server can still start and show a
        # useful setup error when the optional MCP dependency is unavailable.
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        stack = AsyncExitStack()
        try:
            auth = self._build_auth()
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(auth=auth, follow_redirects=True)
            )
            transport = streamable_http_client(self.server_url, http_client=http_client)
            streams = await stack.enter_async_context(transport)
            read_stream, write_stream = streams[:2]
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            return session, stack
        except BaseException:
            await stack.aclose()
            raise

    async def _close_session(self, resources: tuple[Any, AsyncExitStack]) -> None:
        await resources[1].aclose()

    async def _probe(self) -> None:
        resources: tuple[Any, AsyncExitStack] | None = None
        try:
            resources = await self._client_session()
            session = resources[0]
            tools = await session.list_tools()
            names = {str(tool.name) for tool in tools.tools}
            required = {"note_create", "note_update", "note_detail"}
            missing = sorted(required - names)
            if missing:
                raise RuntimeError(f"IdeaShell MCP 缺少工具：{', '.join(missing)}")
            self.connected = True
            self.error = ""
        except Exception as exc:
            self.connected = False
            self.error = str(exc)
        finally:
            if resources is not None:
                await self._close_session(resources)

    async def start_connect(self, callback_url: str) -> dict[str, Any]:
        self._callback_url = str(callback_url or "").strip()
        if not self._callback_url.startswith("http://127.0.0.1:") and not self._callback_url.startswith("http://localhost:"):
            raise ValueError("IdeaShell OAuth 回调必须使用本机地址")

        # A successful probe is enough for subsequent push/pull calls.  Do not
        # restart OAuth on every button click: doing so clears the connected
        # state and can replace a valid session with a second authorization
        # flow during an otherwise ordinary push or pull.
        if self.connected and self.storage.tokens_valid_for():
            return self.status()
        if self._connect_task is None or self._connect_task.done():
            self.authorization_url = ""
            self.error = ""
            self.connected = False
            self._redirect_ready = asyncio.Event()
            self._connect_task = asyncio.create_task(self._probe())
        try:
            await asyncio.wait_for(
                asyncio.shield(self._wait_for_connection_event()), timeout=8
            )
        except asyncio.TimeoutError:
            pass
        return self.status()

    async def _wait_for_connection_event(self) -> None:
        if self._connect_task is None:
            return
        redirect_task = asyncio.create_task(self._redirect_ready.wait())
        done, pending = await asyncio.wait(
            {redirect_task, self._connect_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            if task is redirect_task:
                task.cancel()

    def status(self) -> dict[str, Any]:
        running = self._connect_task is not None and not self._connect_task.done()
        if self.connected:
            status = "connected"
        elif self.error:
            status = "error"
        elif self.authorization_url:
            status = "authorization_required"
        elif running:
            status = "connecting"
        else:
            status = "saved" if self.storage.has_saved_tokens() else "disconnected"
        return {
            "status": status,
            "connected": self.connected,
            "authorization_url": self.authorization_url if status == "authorization_required" else "",
            "error": self.error,
            "server_url": self.server_url,
        }

    def deactivate(self) -> None:
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        self.connected = False
        self.authorization_url = ""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.connected:
            raise RuntimeError("请先连接并授权 IdeaShell")
        async with self._call_lock:
            resources: tuple[Any, AsyncExitStack] | None = None
            try:
                resources = await self._client_session()
                result = await resources[0].call_tool(name, arguments)
                if getattr(result, "isError", False) or getattr(result, "is_error", False):
                    raise RuntimeError(_tool_text(result) or f"IdeaShell {name} 调用失败")
                return _tool_payload(result)
            finally:
                if resources is not None:
                    await self._close_session(resources)


def _tool_text(result: Any) -> str:
    return "\n".join(
        str(getattr(item, "text", ""))
        for item in getattr(result, "content", [])
        if str(getattr(item, "text", "")).strip()
    ).strip()


def _tool_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if not isinstance(structured, dict):
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    text = _tool_text(result)
    if text:
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
            return {"items": value}
        except json.JSONDecodeError:
            return {"text": text}
    return {}


class IdeaShellManager:
    def __init__(self) -> None:
        self.connections: dict[str, IdeaShellConnection] = {}
        self.active_url: str | None = None

    def connection(self, server_url: str) -> IdeaShellConnection:
        url = normalize_server_url(server_url)
        if self.active_url and self.active_url != url:
            previous = self.connections.get(self.active_url)
            if previous is not None:
                previous.deactivate()
        self.active_url = url
        self.connections.setdefault(url, IdeaShellConnection(url))
        return self.connections[url]

    async def deliver_callback(
        self, code: str, state: str | None, iss: str | None
    ) -> None:
        connection = self.connections.get(self.active_url or "")
        if connection is None or not connection.authorization_url or connection.connected:
            raise ValueError("当前没有等待完成的 IdeaShell 授权")
        await connection.deliver_callback(code, state, iss)


ideashell_manager = IdeaShellManager()
