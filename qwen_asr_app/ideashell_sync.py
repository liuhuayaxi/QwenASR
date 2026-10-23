from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from pathlib import Path
from .paths import PRIVATE_ROOT
from .durable import atomic_json
from typing import Any
from urllib.parse import urlparse

IDEASHELL_SERVERS = {
    "china": "https://mcp.ideashell.cn/mcp",
    "global": "https://api.ideashell.com/ideashell/mcp",
}
LEGACY_CHINA_SERVER = "https://api.ideashell.cn/ideashell/mcp"


def normalize_server_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url == LEGACY_CHINA_SERVER:
        url = IDEASHELL_SERVERS["china"]
    if url not in IDEASHELL_SERVERS.values():
        raise ValueError("IdeaShell 服务地址必须选择中国大陆或全球区域")
    return url


def same_server(left: str, right: str) -> bool:
    """Compare saved links and upload receipts across the mainland migration."""
    try:
        return normalize_server_url(left) == normalize_server_url(right)
    except ValueError:
        return False


class FileTokenStorage:
    """Persist MCP OAuth tokens and dynamic client registration with user-only access."""

    def __init__(self, server_url: str) -> None:
        url = normalize_server_url(server_url)
        # Keep one credential store for this region so old and new URLs cannot
        # race to refresh separate copies of the same refresh token.
        host = urlparse(LEGACY_CHINA_SERVER if url == IDEASHELL_SERVERS["china"] else url).hostname
        self.path = PRIVATE_ROOT / f"{host}-oauth.json"

    def oauth_resource_url(self, server_url: str) -> str:
        """Retain the issuer/resource binding of an existing mainland grant.

        The new transport accepts existing grants, but its discovery document
        still advertises the old resource. Existing registrations must refresh
        with their original issuer; never relabel a registration or disable the
        SDK's resource/issuer checks. New grants use normal new-URL discovery.
        """
        url = normalize_server_url(server_url)
        saved = self._read()
        discovery = saved.get("discovery") or {}
        client = saved.get("client_info") or {}
        if (url == IDEASHELL_SERVERS["china"]
                and client.get("issuer") == "https://api.ideashell.cn"
                and discovery.get("server_url") == LEGACY_CHINA_SERVER
                and discovery.get("auth_server_url") == "https://api.ideashell.cn"):
            return LEGACY_CHINA_SERVER
        return url

    def retire_stale_grant(self) -> Path | None:
        """Move a grant bound to another OAuth resource aside (kept as a backup)
        so the next connect starts a fresh authorization for the current URL."""
        if not self.path.exists():
            return None
        backup = self.path.with_name(f"{self.path.stem}.stale-{time.strftime('%Y%m%d-%H%M%S')}.json")
        os.replace(self.path, backup)
        return backup

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write(self, payload: dict[str, Any]) -> None:
        if payload.get("tokens") and "tokens_saved_at" not in payload:
            try:
                payload["tokens_saved_at"] = self.path.stat().st_mtime
            except FileNotFoundError:
                payload["tokens_saved_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        # atomic_json creates its temporary file with mode 0600 before writing.
        atomic_json(self.path, payload)

    def get_discovery(self) -> dict[str, Any]:
        value = self._read().get("discovery")
        return value if isinstance(value, dict) else {}

    def set_discovery(self, value: dict[str, Any]) -> None:
        payload = self._read()
        payload["discovery"] = value
        self._write(payload)

    def callback_url(self) -> str:
        payload = self._read()
        callbacks = (payload.get("client_info") or {}).get("redirect_uris") or []
        return str(payload.get("callback_url") or (callbacks[0] if callbacks else ""))

    def save_callback_url(self, value: str) -> None:
        payload = self._read()
        if payload.get("callback_url") != value:
            payload["callback_url"] = value
            self._write(payload)

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
        tokens = self._read().get("tokens")
        return isinstance(tokens, dict) and bool(tokens.get("access_token") or tokens.get("refresh_token"))

    def tokens_valid_for(self, minimum_seconds: float = 30.0) -> bool:
        payload = self._read()
        raw = payload.get("tokens")
        if not isinstance(raw, dict) or not str(raw.get("access_token") or "").strip():
            return False
        remaining = self._token_remaining(payload)
        return remaining is None or remaining > minimum_seconds


def leaf_errors(exc: BaseException) -> list[BaseException]:
    """Flatten anyio/asyncio exception groups to the errors that caused them."""
    inner = getattr(exc, "exceptions", None)
    if inner:
        return [leaf for item in inner for leaf in leaf_errors(item)]
    return [exc]


def resource_mismatch(exc: BaseException) -> bool:
    return any(type(e).__name__ == "OAuthFlowError" and "does not match expected" in str(e) for e in leaf_errors(exc))


def readable_error(exc: BaseException) -> str:
    leaves = leaf_errors(exc)
    if resource_mismatch(exc):
        return "闪念贝壳已更换授权地址，原授权不再可用，请点击连接重新授权"
    if any(type(e).__name__ in {"ConnectError", "ConnectTimeout", "ReadTimeout"} for e in leaves):
        return "暂时无法连接闪念贝壳服务器，请检查网络后重试"
    message = "；".join(dict.fromkeys(str(e) for e in leaves if str(e)))
    return message or "暂时无法连接闪念贝壳，本机凭证已保留"


CLOSE_TIMEOUT_SECONDS = 10


class IdeaShellConnection:
    def __init__(self, server_url: str) -> None:
        self.server_url = normalize_server_url(server_url)
        self.storage = FileTokenStorage(server_url)
        self.authorization_url = ""
        self.error = ""
        self.connected = False
        self._callback_url = self.storage.callback_url() or f"http://127.0.0.1:{os.getenv('QWEN_ASR_PORT', '8765')}/api/ideashell/oauth/callback"
        self._interactive_auth = False
        self._redirect_ready = asyncio.Event()
        self._callback_queue: asyncio.Queue[dict[str, str | None]] = asyncio.Queue(maxsize=1)
        self._connect_task: asyncio.Task[Any] | None = None
        self._call_lock = asyncio.Lock()

    async def _redirect_handler(self, auth_url: str) -> None:
        self.connected = False
        if not self._interactive_auth:
            self.error = "闪念贝壳授权已失效，请点击连接后重新授权"
            raise RuntimeError(self.error)
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
        from .ideashell_auth import PersistentOAuthProvider
        from mcp.shared.auth import OAuthClientMetadata
        from pydantic import AnyUrl

        return PersistentOAuthProvider(
            server_url=self.storage.oauth_resource_url(self.server_url),
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
            # A split recording sends two encoded AAC files in one note update
            # (often 50–70 MB after base64 encoding). The httpx default timeout
            # is short enough to turn a valid upload into an opaque anyio
            # "unhandled errors in a TaskGroup" during stream shutdown.
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    auth=auth,
                    follow_redirects=True,
                    timeout=httpx2.Timeout(300.0),
                )
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
        # Session teardown (DELETE + draining the GET stream) can hang for the
        # full 300 s HTTP timeout on IdeaShell's side; the call result is
        # already in hand, so bound it instead of stalling every call.
        started = time.monotonic()
        closer = asyncio.ensure_future(resources[1].aclose())
        done, _ = await asyncio.wait({closer}, timeout=CLOSE_TIMEOUT_SECONDS)
        if not done:
            closer.cancel()
            closer.add_done_callback(lambda task: task.cancelled() or task.exception())
        elif closer.exception() is not None:
            print(f"IdeaShell session close error: {readable_error(closer.exception())}", flush=True)
        print(f"IdeaShell session close: {time.monotonic() - started:.1f}s{' (abandoned)' if not done else ''}", flush=True)

    async def _probe(self) -> None:
        async with self._call_lock:
            self._interactive_auth = True
            try:
                await self._probe_locked()
            finally:
                self._interactive_auth = False

    async def _probe_locked(self, retried: bool = False) -> None:
        resources: tuple[Any, AsyncExitStack] | None = None
        try:
            resources = await self._client_session()
            session = resources[0]
            names = set(await self._tool_schemas(session))
            required = {"note_create", "note_update", "note_detail"}
            missing = sorted(required - names)
            if missing:
                raise RuntimeError(f"IdeaShell MCP 缺少工具：{', '.join(missing)}")
            self.connected = True
            self.authorization_url = ""
            self.error = ""
        except Exception as exc:
            self.connected = False
            self.authorization_url = ""
            if resource_mismatch(exc) and not retried and self.storage.retire_stale_grant():
                # The saved grant belongs to a previous resource URL; start a
                # fresh authorization against the current one.
                resources = None
                await self._probe_locked(retried=True)
                return
            self.error = readable_error(exc)
        finally:
            if resources is not None:
                await self._close_session(resources)

    async def start_connect(self, callback_url: str, *, wait_seconds: float = 8) -> dict[str, Any]:
        callback_url = str(callback_url or self._callback_url).strip()
        parsed = urlparse(callback_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/api/ideashell/oauth/callback":
            raise ValueError("IdeaShell OAuth 回调必须使用本机地址")
        if self._connect_task is None or self._connect_task.done():
            self._callback_url = callback_url
            self.storage.save_callback_url(callback_url)

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
            while not self._callback_queue.empty():
                self._callback_queue.get_nowait()
            self._connect_task = asyncio.create_task(self._probe())
        if wait_seconds <= 0:
            return self.status()
        try:
            await asyncio.wait_for(
                asyncio.shield(self._wait_for_connection_event()), timeout=wait_seconds
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
            "has_saved_credentials": self.storage.has_saved_tokens(),
        }

    def deactivate(self) -> None:
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        self.connected = False
        self.authorization_url = ""

    async def list_tools(self) -> dict[str, Any]:
        """Read current attachment schemas instead of assuming a document field."""
        await self.ensure_connected()
        async with self._call_lock:
            resources = await self._client_session()
            try:
                return await self._tool_schemas(resources[0])
            finally:
                await self._close_session(resources)

    @staticmethod
    async def _tool_schemas(session) -> dict[str, Any]:
        from mcp.types import PaginatedRequestParams
        result, cursors = {}, set()
        cursor = None
        while True:
            page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor)) if cursor else await session.list_tools()
            result.update({tool.name: tool.input_schema for tool in page.tools})
            cursor = page.next_cursor
            if not cursor:
                return result
            if cursor in cursors:
                raise RuntimeError("闪念贝壳工具列表返回了重复分页，请稍后重试")
            cursors.add(cursor)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_connected()
        async with self._call_lock:
            resources: tuple[Any, AsyncExitStack] | None = None
            try:
                started = time.monotonic()
                resources = await self._client_session()
                opened = time.monotonic()
                try:
                    result = await resources[0].call_tool(name, arguments)
                finally:
                    # Latency diagnostics: session setup vs. the tool call itself.
                    print(f"IdeaShell call {name}: session {opened - started:.1f}s, call {time.monotonic() - opened:.1f}s, "
                          f"payload {len(json.dumps(arguments, ensure_ascii=False)) // 1024}KB", flush=True)
                if getattr(result, "isError", False) or getattr(result, "is_error", False):
                    raise RuntimeError(_tool_text(result) or f"IdeaShell {name} 调用失败")
                return _tool_payload(result)
            finally:
                if resources is not None:
                    await self._close_session(resources)

    async def ensure_connected(self) -> None:
        if self.connected:
            return
        if not self.storage.has_saved_tokens():
            raise RuntimeError("请先连接并授权 IdeaShell")
        status = await self.start_connect(self._callback_url)
        if status["status"] == "connecting":
            try:
                await asyncio.wait_for(asyncio.shield(self._wait_for_connection_event()), timeout=30)
            except asyncio.TimeoutError:
                pass
            status = self.status()
        if not status["connected"]:
            if status["authorization_url"]:
                raise RuntimeError("闪念贝壳授权已失效，请打开授权页面完成登录")
            raise RuntimeError(status["error"] or "正在恢复闪念贝壳连接，请稍后重试")


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
