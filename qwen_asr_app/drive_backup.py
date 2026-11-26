"""Private, resumable Google Drive copies of selected library documents.

Only document allowlists enter the cloud inventory. OAuth and upload checkpoints
stay under PRIVATE_ROOT; no credentials or upload URLs are returned by status.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import getproxies

import httpx
from fastapi import APIRouter, HTTPException, Request

from .durable import atomic_json
from .media_retention import file_worker
from .paths import PRIVATE_ROOT
from .storage import local_now

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
CHUNK = 4 * 1024 * 1024
TRANSCRIPTS = {"transcript.txt", "原始转写.txt", "AI修正后文本.txt", "transcript.json"}
NOTES = {"课堂笔记.md", "AI总结.md"}
EXCLUDED = {"private", "backups", "compat-backups", "repair-backups", "缓存", "历史备份", "node_modules", "models"}
DEFAULTS = {"pdf": True, "transcripts": True, "notes": False, "yuketang": True, "automatic": False, "interval_minutes": 10}
# 雨课堂题库: questions, answers, explanations, vision transcripts and slide
# images plus the course bindings. Never the browser profile (cookies), the
# job log, or raw API payloads (raw/ holds the student's name and number).
YUKETANG_TOP = {"bindings.json", "courses.json"}
YUKETANG_DIRS = {"questions", "activities", "vision", "images"}
YUKETANG_SUFFIXES = {".json", ".jpg", ".jpeg", ".png", ".webp", ".gif"}


def yuketang_root():
    from .yuketang.store import BankStore
    return Path(BankStore().root)


def yuketang_selected(relative_parts):
    """relative_parts: path parts below the bank root."""
    if len(relative_parts) == 1:
        return relative_parts[0] in YUKETANG_TOP
    if len(relative_parts) < 3 or relative_parts[0] != "classrooms" or not relative_parts[1].isdigit():
        return False
    if len(relative_parts) == 3:
        return relative_parts[2] == "classroom.json"
    return relative_parts[2] in YUKETANG_DIRS and Path(relative_parts[-1]).suffix.lower() in YUKETANG_SUFFIXES
ACTIVE = {"scanning", "uploading", "verifying", "stopping"}


class DriveError(Exception):
    pass


class Stopped(Exception):
    pass


def stamp(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise DriveError("文件不再是普通文件，请重新扫描")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def selected(path, options):
    return ((options["pdf"] and path.suffix.lower() == ".pdf")
            or (options["transcripts"] and path.name in TRANSCRIPTS)
            or (options["notes"] and path.suffix.lower() == ".md"
                and (path.name in NOTES or "AI生成笔记" in path.parts)))


def inventory(settings, recordings, notes, options):
    """Enumerate documents without following links, reading secrets or hashing PDFs."""
    root = Path(settings.storage_root).resolve()
    result, seen = [], set()

    def add(path, relative, anchor, chosen=None):
        if not (selected(path, options) if chosen is None else chosen) or path in seen:
            return
        # Reject linked ancestors as well as a linked file.
        if any(part.is_symlink() for part in (path, *path.parents) if part != anchor.parent):
            return
        value = stamp(path)
        seen.add(path)
        result.append({"path": path, "anchor": anchor, "relative": relative,
                       "key": hashlib.sha256(relative.encode()).hexdigest(),
                       "stamp": value, "size": value[2]})

    def walk(folder, prefix):
        if folder.is_symlink() or not folder.is_dir():
            return
        def failed(error):
            raise DriveError("有资料目录无法读取，请检查本地文件权限") from error
        for directory, dirs, files in os.walk(folder, followlinks=False, onerror=failed):
            dirs[:] = sorted(d for d in dirs if (not d.startswith('.') or d == '.note-pdfs') and d not in EXCLUDED
                             and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                if name.startswith('.'):
                    continue
                path = Path(directory) / name
                try:
                    add(path, prefix + '/' + path.relative_to(folder).as_posix(), folder)
                except FileNotFoundError:
                    continue

    if not root.is_dir():
        raise DriveError("资料库目录不可用，请检查资料库位置")
    walk(root, "资料库")
    for record in recordings.list_records():
        folder = Path(record.directory).absolute()
        if folder.is_relative_to(root):
            continue
        ident = hashlib.sha256(str(folder).encode()).hexdigest()[:12]
        walk(folder, f"外部录音/{folder.name}-{ident}")
    if options.get("yuketang"):
        bank = yuketang_root().absolute()
        if bank.is_dir() and not bank.is_symlink() and not (bank := bank.resolve()).is_relative_to(root):
            for directory, dirs, files in os.walk(bank, followlinks=False):
                here = Path(directory)
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (here / d).is_symlink()
                                 and d not in {"browser-profile", "raw"})
                for name in sorted(files):
                    path = here / name
                    parts = path.relative_to(bank).parts
                    if name.startswith('.') or not yuketang_selected(parts):
                        continue
                    try:
                        add(path, "雨课堂题库/" + "/".join(parts), bank, chosen=True)
                    except FileNotFoundError:
                        continue
    if options["notes"]:
        for note in notes.notes():
            path = Path(note["path"]).absolute()
            if path.is_relative_to(root) or not path.exists():
                continue
            ident = hashlib.sha256(str(path).encode()).hexdigest()[:12]
            add(path, f"外部笔记/{ident}/{path.name}", path.parent)
    return sorted(result, key=lambda item: item["relative"])


def snapshot(item, destination):
    """A stable local copy avoids mixing revisions while a transcript is saved."""
    path = item["path"]
    if any(p.is_symlink() for p in (path, *path.parents)) or stamp(path) != item["stamp"]:
        raise DriveError("文件正在变化，本次跳过；下次备份会重试")
    md5, sha = hashlib.md5(), hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source, destination.open("wb") as target:
        current = os.fstat(source.fileno())
        if [current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns] != item["stamp"]:
            raise DriveError("文件正在变化，本次跳过；下次备份会重试")
        while chunk := source.read(CHUNK):
            target.write(chunk)
            md5.update(chunk)
            sha.update(chunk)
    if stamp(path) != item["stamp"]:
        raise DriveError("文件正在变化，本次跳过；下次备份会重试")
    return {"md5": md5.hexdigest(), "sha256": sha.hexdigest()}


class DriveBackup:
    def __init__(self, settings, recordings, notes, *, directory=None, transport=None, can_run=lambda: True):
        self.settings, self.recordings, self.notes = settings, recordings, notes
        self.directory = Path(directory or PRIVATE_ROOT / "google-drive-backup")
        self.path = self.directory / "state.json"
        self.transport = transport
        self.can_run = can_run
        self.state = {"options": dict(DEFAULTS), "accounts": {}}
        self.load_error = None
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text())
                if not isinstance(state, dict) or not isinstance(state.get("accounts"), dict):
                    raise ValueError()
                options = {**DEFAULTS, **state.get("options", {})}
                if (any(type(options[k]) is not bool for k in DEFAULTS if k != "interval_minutes")
                        or type(options["interval_minutes"]) is not int
                        or not 5 <= options["interval_minutes"] <= 1440
                        or (state.get("job") is not None and not isinstance(state["job"], dict))
                        or not isinstance(state.get("tokens", {}), dict)
                        or not isinstance(state.get("account", {}), dict)):
                    raise ValueError()
                for account in state["accounts"].values():
                    if (not isinstance(account, dict) or not isinstance(account.get("files"), dict)
                            or not isinstance(account.get("folders"), dict)
                            or any(not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                                   for entry in [*account["files"].values(), *account["folders"].values()])):
                        raise ValueError()
                self.state = {**self.state, **state, "options": options}
            except (OSError, ValueError, TypeError):
                self.load_error = "云盘备份状态文件无法读取，已停止备份，请保留原文件并修复"
        self.job = self.state.get("job")
        if self.job and self.job.get("status") in ACTIVE:
            self.job = {**self.job, "status": "interrupted", "message": "服务曾中断，点击备份将从已保存进度继续"}
        self.task = self.loop_task = self.auth_timer = None
        self.auth_server = None
        self.pending_auth = None
        self.auth_task = None
        self.stop_requested = False
        self.closing = False
        self.next_run = time.monotonic() + 60
        self.preview_result = None
        self.operation_lock = asyncio.Lock()
        self.schedule_changed = asyncio.Event()

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def save(self):
        if self.load_error:
            raise DriveError(self.load_error)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        atomic_json(self.path, {**self.state, "job": self.job})
        self.schedule_changed.set()

    def public(self):
        account_id = self.state.get("account_id")
        account = self.state["accounts"].get(account_id, {})
        tokens = self.state.get("tokens", {})
        return {"configured": bool(self.state.get("client")),
                "connected": bool(tokens.get("refresh_token")) and not self.state.get("auth_error"),
                "account": self.state.get("account", {}), "busy": self.busy,
                "authorizing": self.pending_auth is not None,
                "error": self.load_error or self.state.get("auth_error"),
                "options": dict(self.state["options"]), "job": copy.deepcopy(self.job),
                "last_success": account.get("last_success"),
                "folder_url": ("https://drive.google.com/drive/folders/" + account["root_id"])
                              if account.get("root_ready") else None,
                "preview": copy.deepcopy(self.preview_result)}

    def require_idle(self):
        if self.load_error:
            raise DriveError(self.load_error)
        if self.busy or self.pending_auth:
            raise DriveError("备份或账号授权正在进行，请完成后再修改")

    def configure(self, payload):
        self.require_idle()
        new_client = None
        if "credentials" in payload:
            raw = payload["credentials"]
            client = raw.get("installed") if isinstance(raw, dict) else None
            if (not isinstance(client, dict) or not isinstance(client.get("client_id"), str)
                    or not client["client_id"].endswith(".apps.googleusercontent.com")
                    or not isinstance(client.get("client_secret"), str) or not client["client_secret"]):
                raise DriveError("请导入 Google Cloud 创建的“桌面应用”OAuth 客户端 JSON 文件")
            new_client = {key: client[key] for key in ("client_id", "client_secret")}
        options = payload.get("options", {})
        if not isinstance(options, dict):
            raise DriveError("备份选项格式无效")
        updated = dict(self.state["options"])
        for name in DEFAULTS:
            if name not in options:
                continue
            value = options[name]
            if name == "interval_minutes":
                if type(value) is not int or not 5 <= value <= 1440:
                    raise DriveError("自动备份间隔应为 5 到 1440 分钟")
            elif type(value) is not bool:
                raise DriveError("备份选项必须为开关值")
            updated[name] = value
        if not any(updated[k] for k in ("pdf", "transcripts", "notes")):
            raise DriveError("请至少选择一种备份内容")
        if new_client:
            if self.state.get("client") != new_client:
                self.state.pop("tokens", None)
                self.state.pop("account", None)
                self.state.pop("account_id", None)
            self.state["client"] = new_client
            self.state.pop("auth_error", None)
        self.state["options"] = updated
        self.preview_result = None
        self.save()
        self.next_run = time.monotonic() + updated["interval_minutes"] * 60

    def http(self):
        proxy = None if self.transport else getproxies().get("https")
        return httpx.AsyncClient(transport=self.transport, proxy=proxy, follow_redirects=False,
                                 timeout=httpx.Timeout(60, connect=15))

    async def begin_auth(self):
        self.require_idle()
        if not self.state.get("client"):
            raise DriveError("请先导入桌面应用 OAuth 客户端 JSON")
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        self.auth_server = await asyncio.start_server(self.callback, "127.0.0.1", 0, limit=16384)
        port = self.auth_server.sockets[0].getsockname()[1]
        redirect = f"http://127.0.0.1:{port}/"
        self.pending_auth = {"state": state, "verifier": verifier, "redirect": redirect}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.auth_timer = asyncio.create_task(self.expire_auth())
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self.state["client"]["client_id"], "redirect_uri": redirect,
            "response_type": "code", "scope": SCOPE, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent select_account"})

    async def expire_auth(self):
        await asyncio.sleep(600)
        if self.pending_auth:
            self.state["auth_error"] = "授权等待超时，请重新连接 Google Drive"
            self.save()
            await self.end_auth()

    async def end_auth(self):
        self.pending_auth = None
        if self.auth_server:
            self.auth_server.close()
            await self.auth_server.wait_closed()
            self.auth_server = None
        if self.auth_timer and self.auth_timer is not asyncio.current_task():
            self.auth_timer.cancel()
            await asyncio.gather(self.auth_timer, return_exceptions=True)
        self.auth_timer = None

    async def callback(self, reader, writer):
        valid = False
        message = "授权请求无效，请回到 QwenASR 重新连接。"
        status = 400
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, _ = header.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            url = urlsplit(target)
            query = parse_qs(url.query)
            pending = self.pending_auth
            valid = (method == 'GET' and url.path == '/' and pending is not None
                     and hmac.compare_digest(query.get("state", [""])[0], pending["state"]))
            if valid:
                # Consume the state before any network await, preventing replay.
                self.pending_auth = {**pending, "state": secrets.token_urlsafe(32)}
                self.auth_task = asyncio.current_task()
                if "error" in query:
                    raise DriveError("你取消了 Google 授权，可随时重新连接")
                code = query.get("code", [""])[0]
                if not code:
                    raise DriveError("Google 未返回授权码，请重新连接")
                async with self.http() as client:
                    response = await client.post(TOKEN, data={**self.state["client"], "code": code,
                        "code_verifier": pending["verifier"], "redirect_uri": pending["redirect"],
                        "grant_type": "authorization_code"})
                    if response.status_code != 200:
                        raise DriveError("Google 授权交换失败，请检查 OAuth 配置后重试")
                    token = response.json()
                    if (not token.get("refresh_token") or not token.get("access_token")
                            or SCOPE not in token.get("scope", "").split()):
                        raise DriveError("未获得云盘离线备份权限，请重新连接并允许所需权限")
                    info = await client.get(API + "/about", params={"fields": "user(displayName,emailAddress,permissionId)"},
                                            headers={"Authorization": "Bearer " + token["access_token"]})
                    if info.status_code != 200 or not info.json().get("user", {}).get("permissionId"):
                        raise DriveError("无法验证云盘账号，请确认已启用 Google Drive API")
                    user = info.json()["user"]
                    self.state["tokens"] = {"refresh_token": token["refresh_token"],
                        "access_token": token["access_token"], "expires_at": time.time() + int(token.get("expires_in", 3600))}
                    self.state["account_id"] = user["permissionId"]
                    self.state["account"] = {"name": user.get("displayName", ""), "email": user.get("emailAddress", "")}
                    self.state["accounts"].setdefault(user["permissionId"], {"files": {}, "folders": {}})
                    self.state.pop("auth_error", None)
                    self.save()
                message = "Google Drive 已连接。请回到 QwenASR，先点击“小批量测试”核对备份。"
                status = 200
        except DriveError as exc:
            message = str(exc)
            self.state["auth_error"] = message
        except Exception:
            if valid:
                message = "连接 Google 失败，请检查网络或代理后重新连接。"
                self.state["auth_error"] = message
        finally:
            if valid:
                await self.end_auth()
                self.auth_task = None
                self.save()
            # No request URLs, authorization codes or provider errors are logged.
            try:
                stylesheet = (Path(__file__).resolve().parents[1] / "static" / "status.css").read_text(encoding="utf-8")
                style_hash = base64.b64encode(hashlib.sha256(stylesheet.encode()).digest()).decode("ascii")
                body = (
                    "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    f"<title>QwenASR · Google Drive</title><style>{stylesheet}</style></head>"
                    "<body class='status-window'><main class='status-page'><section class='launch-card'>"
                    f"<h1>Google Drive 备份</h1><p>{html.escape(message)}</p>"
                    "<p>现在可以关闭此页面。</p></section></main></body></html>"
                ).encode()
                writer.write((
                    f"HTTP/1.1 {status} {'OK' if status == 200 else 'Bad Request'}\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                    f"Content-Security-Policy: default-src 'none'; style-src 'sha256-{style_hash}'; frame-ancestors 'none'\r\n"
                    "Connection: close\r\n\r\n"
                ).encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    async def access_token(self, client, force=False):
        token = self.state.get("tokens", {})
        if not token.get("refresh_token") or self.state.get("auth_error"):
            raise DriveError("Google Drive 需要重新连接授权")
        if not force and token.get("expires_at", 0) > time.time() + 90:
            return token["access_token"]
        response = await client.post(TOKEN, data={**self.state["client"],
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
        if response.status_code in {400, 401}:
            self.state["auth_error"] = "Google 授权已失效，请重新连接；若应用仍处于 Testing，刷新令牌会在 7 天后过期"
            self.save()
            raise DriveError(self.state["auth_error"])
        if response.status_code != 200:
            raise DriveError("暂时无法刷新 Google 授权，请稍后重试")
        data = response.json()
        token.update(access_token=data["access_token"], expires_at=time.time() + int(data.get("expires_in", 3600)))
        if data.get("refresh_token"):
            token["refresh_token"] = data["refresh_token"]
        self.save()
        return token["access_token"]

    async def request(self, client, method, url, *, accepted=(200,), retry=True, **kwargs):
        destination = urlsplit(url)
        if destination.scheme != 'https' or destination.netloc != 'www.googleapis.com':
            raise DriveError("云盘请求地址无效，已停止发送")
        refresh = False
        for attempt in range(4):
            self.check_stop()
            token = await self.access_token(client, force=attempt > 0 and refresh)
            refresh = False
            try:
                response = await client.request(method, url,
                    headers={"Authorization": "Bearer " + token, **kwargs.get("headers", {})},
                    **{k: v for k, v in kwargs.items() if k != "headers"})
            except httpx.HTTPError:
                if not retry or attempt == 3:
                    raise DriveError("连接 Google Drive 中断，进度已保留，请重试") from None
            else:
                if response.status_code in accepted:
                    return response
                if response.status_code == 401:
                    refresh = True
                elif response.status_code not in {408, 429, 500, 502, 503, 504}:
                    reason = ""
                    try:
                        reason = response.json()["error"]["errors"][0]["reason"]
                    except (KeyError, IndexError, ValueError, TypeError):
                        pass
                    if response.status_code == 403 and reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
                        pass
                    else:
                        messages = {"storageQuotaExceeded": "Google Drive 空间不足，请释放空间后重试",
                                    "accessNotConfigured": "请在 Google Cloud 项目中启用 Google Drive API"}
                        raise DriveError(messages.get(reason, f"Google Drive 请求失败（HTTP {response.status_code}），请检查权限或重新连接"))
                if not retry or attempt == 3:
                    raise DriveError("Google Drive 暂时无法完成请求，进度已保留，请重试")
            await asyncio.sleep(min(2 ** attempt, 8))
        raise DriveError("Google Drive 请求未完成")

    def check_stop(self):
        if self.stop_requested or self.closing:
            raise Stopped()

    async def new_id(self, client):
        response = await self.request(client, "GET", API + "/files/generateIds", params={"count": 1, "space": "drive", "type": "files"})
        return response.json()["ids"][0]

    async def metadata(self, client, ident):
        response = await self.request(client, "GET", API + "/files/" + ident, accepted=(200, 404),
            params={"fields": "id,size,md5Checksum,trashed,mimeType,parents"})
        return response.json() if response.status_code == 200 else None

    async def folder(self, client, account, relative=""):
        mapping = account["folders"]
        if relative not in mapping:
            mapping[relative] = {"id": await self.new_id(client)}
            self.save()
        entry = mapping[relative]
        if relative in self.checked_folders:
            return entry["id"]
        current = await self.metadata(client, entry["id"])
        if (current and current.get("trashed")) or (not current and entry.get("created")):
            # Keep the trashed copy intact; create a fresh destination on this run.
            entry = mapping[relative] = {"id": await self.new_id(client)}
            self.save()
            current = None
        if not current:
            metadata = {"id": entry["id"], "name": PurePosixPath(relative).name if relative else "QwenASR备份",
                        "mimeType": "application/vnd.google-apps.folder"}
            if relative:
                parent = str(PurePosixPath(relative).parent)
                metadata["parents"] = [await self.folder(client, account, "" if parent == '.' else parent)]
            await self.request(client, "POST", API + "/files", json=metadata, accepted=(200, 201, 409))
            current = await self.metadata(client, entry["id"])
        if not current or current.get("mimeType") != "application/vnd.google-apps.folder" or current.get("trashed"):
            raise DriveError("云盘备份目录不可用，请重新连接后重试")
        entry["created"] = True
        self.save()
        if not relative:
            account["root_id"], account["root_ready"] = entry["id"], True
            self.save()
        self.checked_folders.add(relative)
        return entry["id"]

    async def preview(self):
        options = dict(self.state["options"])
        files = await file_worker(inventory, self.settings, self.recordings, self.notes, options)
        self.preview_result = {"count": len(files), "bytes": sum(x["size"] for x in files),
                               "files": [x["relative"] for x in files[:40]], "at": local_now()}
        return self.public()

    def start(self, *, sample=False):
        if self.busy:
            return self.public()
        self.require_idle()
        if not self.can_run():
            raise DriveError("日常备份或服务维护正在进行，请稍后启动云盘备份")
        if not self.public()["connected"]:
            raise DriveError("请先连接 Google Drive")
        self.stop_requested = False
        self.job = {"status": "scanning", "started_at": local_now(), "sample": sample,
                    "total": 0, "completed": 0, "uploaded": 0, "skipped": 0,
                    "failed": 0, "errors": [], "current": "", "file_bytes": 0, "file_size": 0}
        self.save()
        self.task = asyncio.create_task(self.run(sample))
        self.task.add_done_callback(lambda _: self.schedule_changed.set())
        return self.public()

    async def upload(self, client, item, entry, parent, snapshot_path, hashes):
        current = await self.metadata(client, entry["id"])
        if current and not current.get("trashed") and current.get("md5Checksum") == hashes["md5"] and int(current.get("size", -1)) == item["size"]:
            return False
        if (current and current.get("trashed")) or (not current and entry.get("verified_at")):
            entry.clear()
            entry["id"] = await self.new_id(client)
            self.save()
            current = None
        session = entry.get("session") if entry.get("pending_sha") == hashes["sha256"] else None
        for restart in range(3):
            if not session:
                metadata = {"name": item["path"].name}
                if not current:
                    metadata.update(id=entry["id"], parents=[parent])
                response = await self.request(client, "PATCH" if current else "POST",
                    UPLOAD + ("/" + entry["id"] if current else ""), accepted=(200, 201, 409),
                    params={"uploadType": "resumable", "fields": "id,size,md5Checksum"}, json=metadata,
                    headers={"X-Upload-Content-Type": mimetypes.guess_type(item["path"].name)[0] or "application/octet-stream",
                             "X-Upload-Content-Length": str(item["size"])})
                if response.status_code == 409:
                    current = await self.metadata(client, entry["id"])
                    if current and current.get("md5Checksum") == hashes["md5"]:
                        return False
                    continue
                session = response.headers.get("Location", "")
                location = urlsplit(session)
                if location.scheme != 'https' or location.netloc != 'www.googleapis.com' or not location.path.startswith('/upload/drive/v3/files'):
                    raise DriveError("Google 返回的续传地址无效，上传已停止")
                entry.update(session=session, pending_sha=hashes["sha256"])
                self.save()
            # Always query a persisted session. This reconciles a lost final response.
            status = await self.request(client, "PUT", session, accepted=(200, 201, 308, 404, 410),
                headers={"Content-Length": "0", "Content-Range": f"bytes */{item['size']}"}, content=b"")
            if status.status_code in {404, 410}:
                session = None
                current = await self.metadata(client, entry["id"])
                if current and current.get("md5Checksum") == hashes["md5"] and int(current.get("size", -1)) == item["size"]:
                    return True
                continue
            if status.status_code in {200, 201}:
                return True
            offset = self.offset(status, item["size"])
            disruptions = 0
            with snapshot_path.open('rb') as source:
                while offset < item["size"] or item["size"] == 0:
                    self.check_stop()
                    source.seek(offset)
                    data = source.read(CHUNK)
                    end = offset + len(data) - 1
                    headers = {"Content-Length": str(len(data)), "Content-Range":
                               f"bytes {offset}-{end}/{item['size']}" if data else "bytes */0"}
                    try:
                        # Do not replay uncertain chunks before querying the server.
                        response = await self.request(client, "PUT", session, accepted=(200, 201, 308, 404, 410),
                                                      retry=False, content=data, headers=headers)
                    except DriveError:
                        disruptions += 1
                        if disruptions > 3:
                            raise
                        await asyncio.sleep(2 ** (disruptions - 1))
                        response = await self.request(client, "PUT", session, accepted=(200, 201, 308, 404, 410),
                            content=b"", headers={"Content-Length": "0", "Content-Range": f"bytes */{item['size']}"})
                    if response.status_code in {200, 201}:
                        self.job["file_bytes"] = item["size"]
                        return True
                    if response.status_code in {404, 410}:
                        session = None
                        break
                    next_offset = self.offset(response, item["size"])
                    if next_offset <= offset:
                        disruptions += 1
                        if disruptions > 3:
                            raise DriveError("云盘上传进度未前进，已保留进度供重试")
                    offset = next_offset
                    self.job["file_bytes"] = offset
                else:
                    raise DriveError("云盘未确认文件上传完成，稍后重试")
        raise DriveError("续传会话已过期，请重试备份")

    @staticmethod
    def offset(response, total):
        value = response.headers.get("Range")
        if not value:
            return 0
        match = re.fullmatch(r"bytes=0-(\d+)", value)
        if not match or int(match[1]) + 1 > total:
            raise DriveError("Google 返回的上传进度无效")
        return int(match[1]) + 1

    async def run(self, sample):
        try:
            files = await file_worker(inventory, self.settings, self.recordings, self.notes, dict(self.state["options"]))
            if sample:
                candidates = sorted(files, key=lambda x: (x["size"] == 0, x["size"], x["relative"]))
                # Exercise both PDF and text MIME types when available.
                chosen = []
                for pdf in (True, False):
                    match = next((x for x in candidates if (x["path"].suffix.lower() == '.pdf') == pdf), None)
                    if match:
                        chosen.append(match)
                files = (chosen + [x for x in candidates if x not in chosen])[:3]
            self.job.update(total=len(files), total_bytes=sum(x["size"] for x in files))
            account = self.state["accounts"][self.state["account_id"]]
            self.checked_folders = set()
            async with self.http() as client:
                for item in files:
                    self.check_stop()
                    self.job.update(status="uploading", current=item["relative"], file_bytes=0, file_size=item["size"])
                    try:
                        entry = account["files"].get(item["key"])
                        # Stat cache avoids re-reading unchanged large PDFs. Verify the cloud copy as well.
                        if entry and entry.get("stamp") == item["stamp"] and entry.get("md5"):
                            remote = await self.metadata(client, entry["id"])
                            if remote and not remote.get("trashed") and remote.get("md5Checksum") == entry["md5"] and int(remote.get("size", -1)) == item["size"]:
                                self.job["skipped"] += 1
                                self.job["completed"] += 1
                                continue
                        with tempfile.TemporaryDirectory(prefix="drive-file-", dir=self.directory) as tmp:
                            snapshot_path = Path(tmp) / "document"
                            hashes = await file_worker(snapshot, item, snapshot_path)
                            if not entry:
                                entry = account["files"][item["key"]] = {"id": await self.new_id(client)}
                                self.save()
                            parent = await self.folder(client, account, str(PurePosixPath(item["relative"]).parent))
                            uploaded = await self.upload(client, item, entry, parent, snapshot_path, hashes)
                            self.job["status"] = "verifying"
                            remote = await self.metadata(client, entry["id"])
                            if (not remote or remote.get("trashed") or remote.get("md5Checksum") != hashes["md5"]
                                    or int(remote.get("size", -1)) != item["size"]):
                                entry.pop("session", None)
                                entry.pop("pending_sha", None)
                                raise DriveError("云端大小或校验值不一致，未标记成功，请重试")
                            entry.update(**hashes, stamp=item["stamp"], relative=item["relative"], verified_at=local_now())
                            entry.pop("session", None)
                            entry.pop("pending_sha", None)
                            self.job["uploaded" if uploaded else "skipped"] += 1
                            self.job["completed"] += 1
                            self.save()
                    except (DriveError, OSError) as exc:
                        self.job["failed"] += 1
                        if len(self.job["errors"]) < 30:
                            self.job["errors"].append({"file": item["relative"], "message": str(exc) if isinstance(exc, DriveError) else "本地文件无法读取或临时空间不足"})
                        self.save()
                        if self.state.get("auth_error"):
                            break
            self.job.update(status="partial" if self.job["failed"] else "completed", finished_at=local_now(), current="")
            if not self.job["failed"] and not sample:
                account["last_success"] = self.job["finished_at"]
        except (Stopped, asyncio.CancelledError):
            self.job.update(status="stopped", message="已暂停，已上传文件和续传进度已保留")
        except Exception as exc:
            self.job.update(status="failed", message=str(exc) if isinstance(exc, DriveError) else "备份未完成，请检查网络、本地文件和可用空间后重试")
        finally:
            self.save()
            self.next_run = time.monotonic() + self.state["options"]["interval_minutes"] * 60

    async def stop(self):
        self.state["options"]["automatic"] = False
        if self.busy:
            self.stop_requested = True
            self.job["status"] = "stopping"
        self.save()

    async def disconnect(self):
        self.require_idle()
        self.state.pop("tokens", None)
        self.state.pop("auth_error", None)
        self.state.pop("account", None)
        self.state.pop("account_id", None)
        self.state["options"]["automatic"] = False
        self.save()

    async def start_loop(self):
        if self.loop_task is None:
            self.loop_task = asyncio.create_task(self.automatic_loop())

    async def automatic_loop(self):
        while True:
            self.schedule_changed.clear()
            enabled = (not self.closing and not self.load_error and not self.busy and not self.pending_auth
                       and self.can_run() and self.state["options"]["automatic"] and self.public()["connected"])
            delay = (max(0, self.next_run - time.monotonic()) if enabled else
                     300 if self.state["options"]["automatic"] else None)
            if delay == 0:
                try:
                    self.start()
                except DriveError:
                    self.next_run = time.monotonic() + 60
                continue
            try:
                if delay is None:
                    await self.schedule_changed.wait()
                else:
                    await asyncio.wait_for(self.schedule_changed.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self.closing = True
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        await self.end_auth()
        if self.auth_task:
            self.auth_task.cancel()
            await asyncio.gather(self.auth_task, return_exceptions=True)
        if self.busy:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def drive_backup_router(manager):
    router = APIRouter(prefix="/api/library/drive")

    @router.get("")
    async def status():
        return manager.public()

    @router.post("/{action}")
    async def action(action: str, request: Request):
        if request.headers.get("X-QwenASR-Client") != "drive-backup":
            raise HTTPException(403, "请从本机 Google Drive 备份设置操作")
        # The application also validates Origin; keep this router safe in isolation.
        origin = request.headers.get("origin")
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, "备份请求来源不受信任")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "备份请求来源不受信任")
        if manager.operation_lock.locked():
            raise HTTPException(409, "正在处理上一项云盘操作，请稍后重试")
        async with manager.operation_lock:
            try:
                if action == "configure":
                    body = await request.body()
                    if len(body) > 32768:
                        raise DriveError("OAuth 配置文件过大")
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise DriveError("配置格式无效")
                    manager.configure(payload)
                elif action == "connect":
                    url = await manager.begin_auth()
                    return {**manager.public(), "authorization_url": url}
                elif action == "cancel-auth":
                    if manager.auth_task:
                        raise DriveError("Google 正在完成授权，请稍候")
                    await manager.end_auth()
                elif action == "disconnect":
                    await manager.disconnect()
                elif action == "preview":
                    return await manager.preview()
                elif action in {"start", "test"}:
                    return manager.start(sample=action == "test")
                elif action == "stop":
                    await manager.stop()
                else:
                    raise HTTPException(404, "未找到云盘操作")
                return manager.public()
            except (DriveError, ValueError) as exc:
                raise HTTPException(400, str(exc) if isinstance(exc, DriveError) else "配置 JSON 格式无效") from None
    return router
