"""Course materials API. Remote uploads only happen after selecting a note and previewing."""
from __future__ import annotations

import asyncio
import base64
import errno
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from .remote_operations import saved_config, freeze, cancel, receipt, execution_lock, LocalComputeGuard, cancel_pending, recover_cancellation, download_result
from threading import Event

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .course_files import CourseFiles, KINDS, MAX_BYTES, digest_file
from .course_file_conversion import convert, capabilities, ENGINE
from .mineru_settings import mineru_settings
from .mineru_timing import MineruTiming, pdf_pages
from .storage import local_now, safe_component
from .paths import PRIVATE_ROOT
from .durable import atomic_json
from .library_layout import shared
from .shared_library import discover_courses
from .conversion_control import ConversionStopped, check_stopped, conversion_worker
from .ideashell_sync import same_server

NOTE_ID = re.compile(r"[a-f0-9]{32}")
MAX_LEGACY_UPGRADES = 200
_RETRYABLE_FILE_ERRNOS = {11, getattr(errno, "EDEADLK", 35), getattr(errno, "EAGAIN", 11)}


def file_access_error(exc):
    if isinstance(exc, OSError) and exc.errno in _RETRYABLE_FILE_ERRNOS:
        return "原件当前仍是云盘占位文件或被文件提供器占用，请在 Finder 中将原件下载到本机后再升级识别"
    return str(exc)


def needs_mineru_upgrade(conversion):
    """Return whether a completed result was produced by the pre-MinerU-4 flow.

    Folder-supplied AI PDFs have ``external=True`` because they were discovered
    while scanning the shared course folder. They are still eligible for an
    upgrade when their engine is absent or is an older engine marker.
    """
    return (isinstance(conversion, dict)
            and conversion.get("status") in {"completed", "stale"}
            and conversion.get("engine") != ENGINE)


def backup_previous_conversion(path, category, item):
    """Keep a recoverable copy before replacing a legacy AI PDF."""
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    folder = PRIVATE_ROOT / "mineru-upgrade-backups" / stamp / safe_component(category, "未分类")
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / path.name
    for attempt in range(6):
        try:
            shutil.copy2(path, backup)
            break
        except OSError as exc:
            if exc.errno not in _RETRYABLE_FILE_ERRNOS or attempt == 5:
                raise
            time.sleep(min(4.0, 0.35 * (2 ** attempt)))
    atomic_json(folder / "metadata.json", {
        "source": str(path),
        "backup": str(backup),
        "category": str(category),
        "asset_id": item.get("id", ""),
        "asset_name": item.get("name", ""),
        "engine": (item.get("conversion") or {}).get("engine"),
        "created_at": local_now(),
    })
    return backup


def already_synced(receipts, target):
    return any(
        receipt.get("status") == "synced"
        and same_server(receipt.get("server_url"), target["server_url"])
        and all(receipt.get(key) == target[key] for key in ("note_id", "sha256"))
        for receipt in receipts
    )


def note_candidates(payload):
    """Extract only explicit note identifiers, never folder/attachment IDs."""
    result = {}

    def add(ident, title):
        if isinstance(ident, str) and NOTE_ID.fullmatch(ident):
            result.setdefault(ident, {"note_id": ident, "title": str(title or "未命名笔记"),
                                     "reason": "闪念贝壳搜索结果"})

    def visit(value):
        if isinstance(value, dict):
            ident = value.get("note_id") or value.get("noteId")
            if not ident and ("title" in value and ("content" in value or "summary" in value)):
                ident = value.get("id")
            add(ident, value.get("title") or value.get("name"))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            # Current MCP search returns expanded Markdown notes, each starting
            # with a title and a separate note_id metadata line. Body headings
            # and folder/attachment IDs must not become candidate notes.
            for title, ident in re.findall(
                r"^#[ \t]+([^\r\n]+)\r?\n(?:[ \t]*\r?\n)*[ \t]*note_id:[ \t]*([a-f0-9]{32})[ \t]*$",
                value, re.MULTILINE,
            ):
                add(ident, title.strip())
            for title, ident in re.findall(r"([^\n]+?)\s*\[note_id:\s*([a-f0-9]{32})\]", value):
                add(ident, title.lstrip("-#* "))
            if value.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(decoded, (dict, list)):
                        visit(decoded)

    visit(payload)
    return list(result.values())


def note_title(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("title"), str):
            return payload["title"]
        for value in payload.values():
            title = note_title(value)
            if title:
                return title
    if isinstance(payload, list):
        for value in payload:
            title = note_title(value)
            if title:
                return title
    if isinstance(payload, str):
        match = re.search(r"(?:^|\n)(?:#\s+|标题[:：]\s*)([^\n]+)", payload)
        return match.group(1) if match else ""
    return ""


def attachment_arguments(schema, filename, data):
    """Only send names actually advertised by the live note_update schema."""
    def resolve(value):
        if "$ref" in value and value["$ref"].startswith("#/"):
            node = schema
            for part in value["$ref"][2:].split("/"):
                node = node[part]
            value = node
        for key in ("anyOf", "oneOf"):
            if key in value:
                value = next((v for v in value[key] if v.get("type") != "null"), {})
                return resolve(value)
        return value

    properties = schema.get("properties", {})
    for field in ("documents", "document_attachments", "files", "attachments"):
        definition = resolve(properties.get(field, {}))
        if definition.get("type") != "array":
            continue
        item_schema = resolve(definition.get("items", {}))
        item_fields = item_schema.get("properties", {})
        name_key = next((k for k in ("file_name", "filename", "name", "fileName") if k in item_fields), None)
        data_key = next((k for k in ("base64", "base64_data", "base64_content", "data", "content_base64", "file_base64", "content") if k in item_fields), None)
        if not name_key or not data_key:
            continue
        item = {name_key: filename, data_key: base64.b64encode(data).decode("ascii")}
        import mimetypes
        for key in ("mime_type", "mimeType", "content_type"):
            if key in item_fields:
                item[key] = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if set(item_schema.get("required", [])) - item.keys():
            continue
        if set(schema.get("required", [])) - {"note_id", field}:
            raise ValueError("当前接口要求同时替换笔记正文，不能仅追加附件；请使用“复制文件”后在闪念贝壳粘贴")
        return {field: [item]}
    raise ValueError("当前闪念贝壳接口未提供可用的文档附件参数；请复制文件后在闪念贝壳粘贴")


class CourseFileAPI:
    def __init__(self, settings, recordings, ideashell, conversion_lock):
        self.store = CourseFiles(settings)
        self.recordings, self.ideashell = recordings, ideashell
        self.conversion_lock = conversion_lock
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.controls = {}
        self.previews = {}
        self.router = APIRouter(prefix="/api/course-files")
        from .vga_jobs import VGAJobs
        self.vga_jobs = VGAJobs(self.store, self.lock, self.tasks)
        self.vga_jobs.routes(self.router, self.guard)
        self.routes()

    def routes(self):
        router, store = self.router, self.store

        @router.post('/{asset_id}/open-pdf')
        async def open_pdf(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            try:
                from .pdf_preview import open_pdf_in_chrome
                _, path = store.file(str(payload.get('category') or ''), asset_id, str(payload.get('variant') or 'original'))
                return await asyncio.to_thread(open_pdf_in_chrome, path)
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.get("")
        async def listing(category: str = "未分类"):
            try:
                discover_courses(store.settings)
                # Saved links and other open tabs may still use a deleted name.
                if category not in store.settings.categories:
                    category = "未分类"
                items = store.list(category)
                current_settings = mineru_settings.read()
                for item in items:
                    state = item.get("conversion", {})
                    item["needs_mineru_upgrade"] = needs_mineru_upgrade(state)
                    item["mineru_settings_changed"] = (state.get("status") == "completed"
                        and state.get("engine") == ENGINE and not state.get("external")
                        and state.get("mineru_settings") != current_settings)
                    if state.get("status") in {"queued", "running", "stopping"} and (category, item["id"]) not in self.tasks:
                        item["conversion"] = {**state, "status": "interrupted", "stage": "上次转换中断，可重新转换"}
                return {"categories": store.settings.categories, "category": category,
                    "directory": str(store.folder(category)), "files": items, "kinds": KINDS,
                    "capabilities": capabilities(), "shared_folder": shared(store.settings)}
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.post("/upload")
        async def upload(request: Request, category: str = Form(...), kind: str = Form("其他"), file: UploadFile = File(...)):
            self.guard(request)
            try:
                with tempfile.TemporaryDirectory(prefix="qwenasr-material-") as raw:
                    temporary = Path(raw) / "upload"
                    size = 0
                    with temporary.open("wb") as output:
                        while chunk := await file.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise ValueError("每个文件最多 200 MB")
                            output.write(chunk)
                    if not size:
                        raise ValueError("不能添加空文件")
                    async with self.lock:
                        item, duplicate = await asyncio.to_thread(store.import_file, category, temporary, file.filename, kind)
                    return {"file": item, "duplicate": duplicate}
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc
            finally:
                await file.close()

        @router.get("/{asset_id}/download")
        async def download(asset_id: str, category: str, variant: str = "original"):
            try:
                _, path = store.file(category, asset_id, variant)
                return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})
            except (ValueError, OSError) as exc:
                raise HTTPException(404, str(exc)) from exc

        @router.post("/{asset_id}/rename")
        async def rename_file(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            category = payload.get('category')
            if not shared(store.settings):
                raise HTTPException(409, '请先启用课程文件夹同步')
            async with self.lock:
                if (category, asset_id) in self.tasks:
                    raise HTTPException(409, '文件正在转换，请完成后再重命名')
                try:
                    item = store.shared_store.rename(category, asset_id, str(payload.get('name') or '').strip())
                    self.previews = {key: value for key, value in self.previews.items() if value.get('asset_id') != asset_id}
                    return {'file': item}
                except (OSError, ValueError) as exc:
                    raise HTTPException(400, str(exc)) from exc

        @router.post("/{asset_id}/delete")
        async def delete_file(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            category = payload.get('category')
            if not shared(store.settings):
                raise HTTPException(409, '请先启用课程文件夹同步')
            async with self.lock:
                if (category, asset_id) in self.tasks:
                    raise HTTPException(409, '文件正在转换，请完成后再删除')
                try:
                    result = store.shared_store.delete(category, asset_id)
                    self.previews = {key: value for key, value in self.previews.items() if value.get('asset_id') != asset_id}
                    return result
                except (OSError, ValueError) as exc:
                    raise HTTPException(400, str(exc)) from exc

        @router.post("/{asset_id}/replace")
        async def replace_file(asset_id: str, request: Request, category: str = Form(...), file: UploadFile = File(...)):
            self.guard(request)
            if not shared(store.settings):
                raise HTTPException(409, '请先启用课程文件夹同步')
            try:
                with tempfile.TemporaryDirectory(prefix='qwenasr-replace-') as temporary:
                    source = Path(temporary) / 'source'
                    size = 0
                    with source.open('wb') as output:
                        while chunk := await file.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise ValueError('每个文件最多 200 MB')
                            output.write(chunk)
                    if not size:
                        raise ValueError('不能使用空文件替换原件')
                    async with self.lock:
                        if (category, asset_id) in self.tasks:
                            raise HTTPException(409, '文件正在转换，请完成后再替换')
                        item, target = store.file(category, asset_id)
                        if Path(file.filename or '').suffix.lower() != target.suffix.lower():
                            raise ValueError('替换文件的格式须与原件一致')
                        # Preserve the previous original in the system Trash.
                        from .storage import TRASH_ROOT
                        archive = TRASH_ROOT / ('替换原件-' + uuid.uuid4().hex) / target.name
                        archive.parent.mkdir(parents=True)
                        shutil.copy2(target, archive)
                        stage = target.with_name('.replace-' + uuid.uuid4().hex)
                        try:
                            shutil.copyfile(source, stage)
                            stage.replace(target)
                        finally:
                            stage.unlink(missing_ok=True)
                        self.previews = {key: value for key, value in self.previews.items() if value.get('asset_id') != asset_id}
                        item, _ = store.get(category, asset_id)
                        return {'file': item, 'replaced': True}
            except (OSError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc
            finally:
                await file.close()

        @router.post("/{asset_id}/clipboard")
        async def clipboard(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            try:
                _, path = store.file(payload.get("category"), asset_id, payload.get("variant", "original"))
                if sys.platform != "darwin":
                    raise ValueError("复制文件到系统剪贴板目前仅支持 Mac；其他设备请下载文件")
                # A bare alias is not a valid clipboard item on macOS and
                # raises -10006. Finder's clipboard accepts a list of file
                # aliases and publishes the promised-file flavors that apps
                # such as IdeaShell can paste.
                script = ('on run argv\n'
                          'set fileAlias to POSIX file (item 1 of argv) as alias\n'
                          'tell application "Finder"\n'
                          'set the clipboard to {fileAlias}\n'
                          'end tell\n'
                          'end run')
                result = await asyncio.to_thread(subprocess.run, ["/usr/bin/osascript", "-e", script, str(path)],
                    capture_output=True, text=True, timeout=20)
                if result.returncode:
                    raise ValueError("未能复制文件：" + result.stderr[-300:])
                return {"copied": True, "name": path.name}
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.post("/reveal")
        async def reveal(request: Request, payload: dict = Body(...)):
            self.guard(request)
            try:
                async with self.lock:
                    folder = store.folder(payload.get("category"), create=True)
                if sys.platform != "darwin":
                    raise ValueError("当前系统请使用显示的课程目录路径")
                await asyncio.to_thread(subprocess.run, ["/usr/bin/open", str(folder)], check=True, timeout=20)
                return {"directory": str(folder)}
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.post("/{asset_id}/convert")
        async def conversion(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            try:
                return await self.start_conversion(payload.get("category"), asset_id)
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.post("/upgrade-legacy")
        async def upgrade_legacy(request: Request, payload: dict = Body(...)):
            """Queue every legacy course result in the selected category."""
            self.guard(request)
            category = payload.get("category")
            try:
                if category not in store.settings.categories:
                    raise ValueError("课程分类已变化，请刷新后重试")
                candidates = [item for item in store.list(category)
                              if needs_mineru_upgrade(item.get("conversion", {}))]
                if len(candidates) > MAX_LEGACY_UPGRADES:
                    raise ValueError(f"单次最多升级 {MAX_LEGACY_UPGRADES} 个旧版结果，请分课程处理")
                queued, already_running = [], []
                for item in candidates:
                    result = await self.start_conversion(category, item["id"])
                    state = (result.get("file") or {}).get("conversion", {})
                    if state.get("status") in {"queued", "running", "stopping"}:
                        queued.append(item["id"])
                    else:
                        already_running.append(item["id"])
                return {"category": category, "queued": len(queued),
                        "already_running": len(already_running),
                        "total": len(candidates)}
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.post("/{asset_id}/matches")
        async def matches(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            try:
                item, _ = store.get(payload.get("category"), asset_id)
                connection = self.ideashell.connection(payload.get("server_url", ""))
                candidates = {}
                for receipt in item.get("sync", []):
                    if same_server(receipt.get("server_url"), connection.server_url):
                        candidates[receipt["note_id"]] = {"note_id": receipt["note_id"], "title": receipt.get("title", "已关联笔记"), "reason": "这个文件此前选择的笔记"}
                for record in self.recordings.list_records():
                    sync = record.metadata.get("ideashell_sync", {})
                    if record.metadata.get("category") == payload.get("category") and same_server(sync.get("server_url"), connection.server_url) and NOTE_ID.fullmatch(sync.get("note_id", "")):
                        candidates.setdefault(sync["note_id"], {"note_id": sync["note_id"], "title": record.metadata.get("title", "课程笔记"), "reason": "本课程已关联的录音笔记"})
                warning = ""
                if payload.get("search"):
                    try:
                        schema = (await connection.list_tools()).get("note_search", {})
                        props = schema.get("properties", {})
                        field = next((k for k in ("query", "keyword", "keywords") if k in props), None)
                        if not field:
                            raise ValueError("当前接口未提供可识别的笔记搜索参数，请输入笔记编号")
                        query = str(payload.get("query") or payload.get("category"))[:200]
                        args = {field: query}
                        if "limit" in props:
                            args["limit"] = 3
                        remote = await connection.call_tool("note_search", args)
                        found = note_candidates(remote)
                        for candidate in found:
                            candidates.setdefault(candidate["note_id"], candidate)
                        if not found:
                            warning = "搜索未返回可匹配的笔记；可修改关键词或粘贴笔记编号。"
                    except Exception as exc:
                        warning = str(exc)
                return {"candidates": list(candidates.values()), "warning": warning}
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.post("/{asset_id}/preview")
        async def preview(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            try:
                category, variant = payload.get("category"), payload.get("variant", "pdf")
                ident = str(payload.get("note_id", "")).strip()
                if not NOTE_ID.fullmatch(ident):
                    raise ValueError("请选择目标笔记或填写完整的 32 位笔记编号")
                item, path = store.file(category, asset_id, variant)
                if path.stat().st_size > 20 * 1024**2:
                    raise ValueError("自动上传暂限 20 MB；较大文件请使用“复制文件”在闪念贝壳粘贴")
                connection = self.ideashell.connection(payload.get("server_url", ""))
                schemas = await connection.list_tools()
                attachment_arguments(schemas.get("note_update", {}), path.name, b"")
                detail = await connection.call_tool("note_detail", {"note_id": ident, "scope": "brief"})
                token = uuid.uuid4().hex
                receipt = {"asset_id": asset_id, "category": category, "variant": variant, "note_id": ident,
                    "server_url": connection.server_url, "title": note_title(detail) or ident,
                    "filename": path.name, "size": path.stat().st_size, "sha256": digest_file(path),
                    "expires": time.time() + 600}
                self.previews = {k: v for k, v in self.previews.items() if v["expires"] > time.time()}
                self.previews[token] = receipt
                duplicate = already_synced(item.get("sync", []), receipt)
                return {"token": token, **receipt, "duplicate": duplicate}
            except (ValueError, OSError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, f"闪念贝壳预览失败：{exc}") from exc

        @router.post("/{asset_id}/sync")
        async def sync(asset_id: str, request: Request, payload: dict = Body(...)):
            self.guard(request)
            async with self.lock:
                preview = self.previews.get(str(payload.get("token", "")))
                if not preview or preview["asset_id"] != asset_id or preview["expires"] < time.time():
                    raise HTTPException(409, "预览已失效，请重新选择笔记并预览")
                try:
                    item, path = store.file(preview["category"], asset_id, preview["variant"])
                    data = path.read_bytes()
                    import hashlib
                    if hashlib.sha256(data).hexdigest() != preview["sha256"]:
                        raise ValueError("文件已变化，请重新预览")
                    if already_synced(item.get("sync", []), preview):
                        return {"synced": True, "duplicate": True}
                    connection = self.ideashell.connection(preview["server_url"])
                    schemas = await connection.list_tools()
                    args = attachment_arguments(schemas.get("note_update", {}), path.name, data)
                    entry = {k: preview[k] for k in ("note_id", "server_url", "title", "filename", "sha256", "variant")}
                    entry.update(status="sending", updated_at=local_now(), operation_id=uuid.uuid4().hex)
                    item, _ = store.get(preview["category"], asset_id)
                    item.setdefault("sync", []).append(entry)
                    store.save(preview["category"], item)
                    try:
                        await connection.call_tool("note_update", {"note_id": preview["note_id"], **args})
                    except BaseException:
                        self.update_receipt(preview["category"], asset_id, entry["operation_id"], "unknown")
                        raise
                    self.update_receipt(preview["category"], asset_id, entry["operation_id"], "synced")
                    self.previews.pop(str(payload.get("token", "")), None)
                    return {"synced": True, "note_id": preview["note_id"]}
                except (ValueError, OSError, RuntimeError) as exc:
                    raise HTTPException(400, str(exc)) from exc
                except Exception as exc:
                    raise HTTPException(502, f"上传结果未确认，请先在闪念贝壳检查附件后再重试：{exc}") from exc

    def update_receipt(self, category, asset_id, operation_id, status):
        # Conversion progress may have changed the manifest while the network awaited.
        item, _ = self.store.get(category, asset_id)
        for receipt in item.get("sync", []):
            if receipt.get("operation_id") == operation_id:
                receipt.update(status=status, updated_at=local_now())
        self.store.save(category, item)

    @staticmethod
    def guard(request):
        if request.headers.get("X-QwenASR-Client") != "course-files":
            raise HTTPException(403, "请从课程资料库页面操作")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "请从当前本机工作台页面操作")

    async def start_conversion(self, category, asset_id, force=False, resume=False, new_attempt=False, provider_override=None):
        async with self.lock:
            item, directory = self.store.get(category, asset_id)
            previous = item.get('conversion', {})
            key = (category, asset_id)
            if key in self.tasks:
                return {'file': item}
            from .inference_providers import inference_settings, runtime_fingerprint
            provider = saved_config(previous.get('inference_settings'), remote_reference=bool(previous.get('operation'))) if resume else inference_settings.for_task('mineru', provider_override)
            if resume and previous.get('operation'):
                source = Path(previous['operation']['blob_path'])
                current_digest = previous['operation']['source_sha256']
            else:
                item, source = self.store.file(category, asset_id)
                current_digest = await asyncio.to_thread(digest_file, source)
                item['sha256'] = current_digest
            legacy_upgrade = needs_mineru_upgrade(previous) or bool(previous.get('legacy_upgrade'))
            settings = previous.get('mineru_settings') if resume else None
            if settings is None:
                settings = mineru_settings.read()
            # A saved completed artifact can be read without any network probe.
            current_runtime = await asyncio.to_thread(runtime_fingerprint, provider) if provider['mineru_provider'] == 'local' else None
            same_provider = previous.get('inference_settings', {}).get('mineru_provider', 'local') == provider['mineru_provider']
            if (not force and previous.get('status') == 'completed' and previous.get('engine') == ENGINE
                    and previous.get('mineru_settings') == settings and same_provider
                    and previous.get('source_sha256') == current_digest
                    and current_runtime is not None and previous.get('runtime_fingerprint') == current_runtime):
                try:
                    _, existing = self.store.file(category, asset_id, 'pdf')
                except ValueError:
                    existing = None
                if existing and await asyncio.to_thread(digest_file, existing) == previous.get('output_sha256'):
                    return {'file': item, 'reused': True}
            operation = previous.get('operation') if resume else None
            if provider['mineru_provider'] != 'local' and operation is None:
                operation = freeze(source, provider, settings, parent_id=asset_id)
            operation_id = operation['operation_id'] if operation else ((previous.get('operation_id') or uuid.uuid4().hex) if resume else uuid.uuid4().hex)
            item["conversion"] = {**previous, "status": "queued", "stage": "等待 MinerU 识别",
                                  "mineru_settings": settings,
                                  "legacy_upgrade": legacy_upgrade,
                                  'source_sha256': current_digest, 'runtime_fingerprint': current_runtime,
                                  'operation_id': operation_id, 'operation': operation,
                                  "inference_settings": provider,
                                  "progress": 0, "progress_estimate": None, "error": "", "external": False,
                                  "task_hidden": False,
                                  "created_at": previous.get("created_at") or local_now(), "updated_at": local_now()}
            self.store.save(category, item)
            stop = Event()
            self.controls[key] = {"stop": stop, "action": "interrupted"}
            task = asyncio.create_task(self.run_conversion(category, asset_id, source, stop))
            self.tasks[key] = task

            def finished(done):
                if self.tasks.get(key) is not done:
                    return
                try:
                    current, _ = self.store.get(category, asset_id)
                    if current.get("conversion", {}).get("status") in {"queued", "running", "stopping"}:
                        current["conversion"].update(status=self.controls[key]["action"],
                                                     stage="识别已停止，可重新识别", updated_at=local_now())
                        self.store.save(category, current)
                except (ValueError, OSError):
                    pass
                self.tasks.pop(key, None)
                self.controls.pop(key, None)

            task.add_done_callback(finished)
            return {"file": item}

    async def conversion_action(self, category, asset_id, action):
        if action in {'continue', 'retry', 'download'}:
            item, _ = self.store.get(category, asset_id)
            state = item.get('conversion', {})
            path = state.get('operation', {}).get('receipt_path') if state.get('operation') else None
            saved = receipt(path)
            if path and cancel_pending(path):
                state['remote_operation'] = await recover_cancellation(path, saved_config(state.get('inference_settings'), remote_reference=True))
                state.update(status='paused', stage='取消状态已对账；旧结果与收据保留')
                self.store.save(category, item)
                return {'file': item}
            if action == 'download':
                state['inference'] = await download_result(path, state['inference_settings'], 'mineru', Path(path).parent/'downloaded-output')
                state.update(status='pending', stage='远程结果已下载；继续可进行本地打包')
                self.store.save(category, item)
                return {'file': item}
            if saved.get('state') in {'failed', 'canceled'}:
                raise ValueError('旧操作已终止；请明确新建识别，旧收据保留')
            return await self.start_conversion(category, asset_id, force=True, resume=True)
        if action in {'new_local', 'new_recognition'}:
            return await self.start_conversion(category, asset_id, force=True,
                provider_override={'mineru_provider': 'local'} if action == 'new_local' else None)
        if action not in {"pause", "cancel"}:
            raise ValueError("无效的任务操作")
        async with self.lock:
            item, _ = self.store.get(category, asset_id)
            key = (category, asset_id)
            target = "paused" if action == "pause" else "cancelled"
            state = item.get("conversion", {})
            if state.get("status") == "completed":
                raise ValueError("这项识别任务已经完成")
            if key in self.tasks:
                control = self.controls[key]
                control.update(action=target)
                control["stop"].set()
                waiting = state.get("status") == "queued"
                state.update(status="stopping", stage="正在停止识别，原件保留")
                if waiting:
                    self.tasks[key].cancel()
            else:
                state.update(status=target, stage="已暂停，可重新识别" if action == "pause" else "已取消，原件保留")
            if state.get('operation'):
                cancel(state['operation']['receipt_path'])
            state['updated_at'] = local_now()
            self.store.save(category, item)
            return {"file": item}

    async def run_conversion(self, category, asset_id, source, stop):
        generation = None
        try:
            item, _ = self.store.get(category, asset_id)
            settings = item["conversion"].get("mineru_settings") or mineru_settings.read()
            provider = saved_config(item['conversion'].get('inference_settings'), remote_reference=bool(item['conversion'].get('operation')))
            generation = item['conversion']['operation_id']
            async with execution_lock(self.conversion_lock, provider, 'mineru'):
                check_stopped(stop)
                loop = asyncio.get_running_loop()

                def update(stage, percent):
                    if stop.is_set():
                        return
                    try:
                        item, _ = self.store.get(category, asset_id)
                        if item['conversion'].get('operation_id') != generation or item['conversion']['status'] not in {'queued', 'running'}:
                            return
                        estimate = timing.observe(stage, percent)
                        item["conversion"].update(status="running", stage=stage, progress=estimate['percent'],
                                                  progress_estimate=estimate, updated_at=local_now())
                        self.store.save(category, item)
                    except (ValueError, OSError):
                        stop.set()

                item, directory = self.store.get(category, asset_id)
                operation = item['conversion'].get('operation')
                if operation:
                    source = Path(operation['blob_path'])
                timing = MineruTiming(settings=settings, config=provider,
                    pages=await asyncio.to_thread(pdf_pages, source), run_id=generation,
                    resumed=bool(operation and receipt(operation['receipt_path']).get('job_id')))
                source_digest = item['conversion']['source_sha256']
                runtime_identity = item['conversion'].get('runtime_fingerprint')
                operation_id = item['conversion'].get('operation_id') or uuid.uuid4().hex
                if await asyncio.to_thread(digest_file, source) != source_digest:
                    raise ValueError("原件已改变，请重新识别")
                output = (self.store.shared_store.output_path(category, item) if shared(self.store.settings)
                          else directory / "PDF" / (Path(item["name"]).stem + "(AI).pdf"))
                previous_state = dict(item.get("conversion", {}))
                legacy_backup = None
                published_output = output
                if operation:
                    output = output.with_name('.' + output.stem + '-' + generation[:12] + output.suffix)
                update('正在准备 MinerU 识别', 1)
                report = await conversion_worker(convert, source, output,
                    lambda stage, percent: loop.call_soon_threadsafe(update, stage, percent), stop=stop, settings=settings,
                    inference_config=provider, receipt_path=PRIVATE_ROOT/'remote-operations'/operation_id/'receipt.json',
                    local_guard=LocalComputeGuard(self.conversion_lock, loop) if provider['mineru_provider'] != 'local' else None)
                item, _ = self.store.get(category, asset_id)
                if item['conversion'].get('operation_id') != generation:
                    return
                if operation:
                    # Keep generation identifiers in hidden work/history paths.
                    # Publishing a new version must not discard the old PDF.
                    if published_output.is_file():
                        previous_digest = digest_file(published_output)
                        legacy_backup = directory / '.qwenasr-files' / 'versions' / asset_id / (previous_digest + '.pdf')
                        legacy_backup.parent.mkdir(parents=True, exist_ok=True)
                        if not legacy_backup.exists():
                            shutil.copy2(published_output, legacy_backup)
                        if digest_file(legacy_backup) != previous_digest:
                            raise ValueError('旧版 PDF 备份校验失败，未替换已有版本')
                    output.replace(published_output)
                    output = published_output
                if legacy_backup:
                    report['legacy_output_backup'] = str(legacy_backup)
                changed = digest_file(source) != source_digest
                report['progress_estimate'] = timing.finish(learn=not changed)
                if provider['mineru_provider'] != 'local':
                    runtime_identity = report.get('inference', {}).get('metrics', {}).get('runtime_fingerprint')
                item["conversion"] = {"status": "stale" if changed else "completed",
                    "stage": "原件在识别时发生变化，请重新识别" if changed else "MinerU 识别完成",
                    "name": output.name, "progress": 100, "created_at": item["conversion"].get("created_at", local_now()),
                    'source_sha256': source_digest, 'output_sha256': digest_file(output),
                    'runtime_fingerprint': runtime_identity, 'operation_id': operation_id, 'operation': operation,
                    "sha256": digest_file(output), "updated_at": local_now(), **report}
                if provider is not None:
                    item['conversion']['inference_settings'] = provider
                if shared(self.store.settings):
                    item['conversion'].update(relative_path=output.relative_to(directory).as_posix(),
                                              stamp=self.store.shared_store._stamp(output))
                self.store.save(category, item)
                from .background_wakeup import library_changes
                library_changes.notify([str(directory / "asset.json")])
                if not changed:
                    from .remote_operations import discard_completed_payloads
                    await asyncio.to_thread(discard_completed_payloads,
                        PRIVATE_ROOT / 'remote-operations' / operation_id / 'receipt.json')
        except (Exception, asyncio.CancelledError) as exc:
            try:
                item, _ = self.store.get(category, asset_id)
            except (ValueError, OSError):
                return
            stopped = isinstance(exc, (ConversionStopped, asyncio.CancelledError))
            if generation is not None and item['conversion'].get('operation_id') != generation:
                return
            status = self.controls[(category, asset_id)]['action'] if stopped else ('pending' if getattr(exc, 'fallback_unsafe', False) else 'error')
            stage = "已停止；继续时重新识别此文件" if stopped else file_access_error(exc)
            item["conversion"].update(status=status, stage=stage, error="" if stopped else file_access_error(exc), updated_at=local_now())
            self.store.save(category, item)
