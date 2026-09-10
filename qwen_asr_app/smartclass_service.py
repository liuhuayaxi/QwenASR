"""Independent local course sync service, so a live recording need not restart."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware

from .smartclass import (STATE_DIR, SmartClassClient, SmartClassError, private_json,
                        course_key, current_term, lesson_groups, extract_audio, connection_error)
from .course_download import stream_audio

ROOT = Path(__file__).resolve().parent.parent
status = {"state": "idle", "message": "请配置统一身份认证账号", "identity": None}
client = SmartClassClient()
job = None
platform_lock = asyncio.Lock()
course_rows = {}
group_rows = {}
stop_event = threading.Event()
WORKBENCH = "http://127.0.0.1:8765"


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


imports = read_json(STATE_DIR / "imports.json", {})
options = read_json(STATE_DIR / "options.json", {})
for item in imports.values():
    if item.get("state") not in {"completed", "error", "interrupted"}:
        item.update(state="interrupted", message="服务已重启，可勾选原课次继续")


def save_imports():
    private_json(STATE_DIR / "imports.json", imports)


async def defaults():
    if options.get("summary_provider"):
        return
    async with httpx.AsyncClient(timeout=15, trust_env=False) as local:
        response = await local.get(WORKBENCH + "/api/library")
        response.raise_for_status()
        records = response.json().get("records", [])
        for record in records:
            provider = record.get("batch_options", {}).get("ai", {}).get("summary_provider")
            if provider and provider.get("model"):
                options["summary_provider"] = {k: v for k, v in provider.items() if k != "api_key"}
                correction = record.get("batch_options", {}).get("ai", {}).get("correction_provider") or provider
                options.setdefault("correction_provider", {k: v for k, v in correction.items() if k != "api_key"})
                break
    options.setdefault("generate_summary", True)
    options.setdefault("ai_correction", True)


async def reconcile_imports():
    """Reflect notes completed later in the workbench, only for imported records."""
    if not imports:
        return
    async with httpx.AsyncClient(timeout=5, trust_env=False) as local:
        r = await local.get(WORKBENCH + "/api/library")
        r.raise_for_status()
        records = {item["id"]: item for item in r.json().get("records", [])}
    changed = False
    for item in imports.values():
        record = records.get(item.get("record_id"))
        if record and record.get("status") == "completed" and record.get("summary_job", {}).get("status") == "completed":
            ident = record["id"]
            item.update(state="completed", message="已归档为一份课堂笔记", progress=100, summary_generated=True,
                        bundle=WORKBENCH + f"/api/recordings/{ident}/download/bundle",
                        summary=WORKBENCH + f"/api/recordings/{ident}/download/ai-summary")
            changed = True
    if changed:
        save_imports()


@asynccontextmanager
async def lifespan(app):
    global job
    saved = read_json(STATE_DIR / "credentials.json", {})
    if saved.get("username") and saved.get("password"):
        job = asyncio.create_task(connect(saved["username"], saved["password"], False))
    yield
    stop_event.set()
    if job and not job.done():
        await job
    client.close()


app = FastAPI(title="课程回放自动转写", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
                   allow_methods=["POST"], allow_headers=["Content-Type", "X-QwenASR-Client"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        origin = request.headers.get("origin")
        bridge = request.url.path == "/api/options" and origin in {"http://127.0.0.1:8765", "http://localhost:8765"}
        if (origin != expected and not bridge) or request.headers.get("x-qwenasr-client") != "smartclass":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "请通过本机课程同步页面操作"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'"
    return response


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: SecretStr
    save: bool = False


@app.get("/", response_class=HTMLResponse)
async def index():
    return (ROOT / "static" / "smartclass.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def get_status():
    return {**status, "credentials_saved": (STATE_DIR / "credentials.json").exists(),
            "term": current_term(), "courses": list(course_rows.values()),
            "imports": list(imports.values()), "options": options}


async def connect(username, password, save):
    course_rows.clear()
    group_rows.clear()
    status.update(state="connecting", message="正在通过南邮统一身份认证登录…", identity=None)
    try:
        async with platform_lock:
            identity = await asyncio.to_thread(client.login, username, password)
        if save:
            private_json(STATE_DIR / "credentials.json", {"username": username, "password": password})
        status.update(state="syncing", message="登录成功，正在获取本学期课程…", identity=identity)
        await load_courses()
        try:
            await defaults()
            await reconcile_imports()
        except (httpx.HTTPError, ValueError):
            pass
    except SmartClassError as exc:
        status.update(state="error", message=str(exc))
    except httpx.HTTPError as exc:
        status.update(state="error", message=connection_error(exc, client.last_stage))
    except Exception:
        status.update(state="error", message="学校响应格式发生变化，需要检查适配")


@app.post("/api/connect")
async def connect_account(data: Credentials):
    global job
    if job and not job.done():
        raise HTTPException(409, "已有任务正在进行")
    password = data.password.get_secret_value()
    if not password or len(password) > 2000:
        raise HTTPException(400, "请输入有效密码")
    job = asyncio.create_task(connect(data.username.strip(), password, data.save))
    return {"started": True}


@app.post("/api/reconnect")
async def reconnect():
    global job
    if job and not job.done():
        raise HTTPException(409, "已有任务正在进行")
    try:
        saved = json.loads((STATE_DIR / "credentials.json").read_text())
    except (OSError, ValueError):
        raise HTTPException(400, "尚未保存本机登录配置")
    job = asyncio.create_task(connect(saved["username"], saved["password"], False))
    return {"started": True}


def check_idle():
    if job and not job.done():
        raise HTTPException(409, "已有任务正在进行，请等待或停止当前任务")


async def load_courses():
    async with platform_lock:
        rows = await asyncio.to_thread(client.courses)
    course_rows.clear()
    group_rows.clear()
    for row in rows:
        course_rows[course_key(row)] = {**row, "key": course_key(row)}
    status.update(state="connected", message=f"本学期 {len(rows)} 条课程记录，选择课程查看回放",
                  course_revision=time.time_ns())


@app.post("/api/refresh")
async def refresh_courses():
    global job
    check_idle()
    if not status.get("identity"):
        raise HTTPException(400, "请先登录")
    async def refresh():
        status.update(state="syncing", message="正在刷新本学期课程…")
        try:
            await load_courses()
        except Exception:
            status.update(state="error", message="刷新课程失败，请重新连接账号后重试")
    job = asyncio.create_task(refresh())
    return {"started": True}


@app.get("/api/lessons")
async def lessons(course: str):
    if course not in course_rows:
        raise HTTPException(404, "请先选择本学期课程")
    try:
        async with platform_lock:
            videos = await asyncio.to_thread(client.videos, course_rows[course])
        groups = lesson_groups(videos, course_rows[course])
        group_rows.update({g["id"]: g for g in groups})
        return {"groups": groups}
    except (SmartClassError, httpx.HTTPError, ValueError):
        raise HTTPException(502, "获取回放失败，请刷新登录状态后重试")


@app.post("/api/options")
async def set_options(request: Request):
    data = await request.json()
    for name in ("summary_provider", "correction_provider"):
        provider = data.get(name) or {}
        if provider:
            allowed = {"base_url", "model", "api_mode", "context_window", "max_output_tokens", "concurrency", "extra_params", "system_prompt"}
            options[name] = {k: v for k, v in provider.items() if k in allowed}
    for key in ("summary_system_prompt", "abstract_system_prompt", "generate_summary", "ai_correction"):
        if key in data:
            options[key] = data[key]
    private_json(STATE_DIR / "options.json", options)
    return {"saved": True}


def category_for(name, profiles):
    def norm(text):
        text = re.sub(r"[（(].*?[）)]", "", text)
        return re.sub(r"(?:双语|[ABC])$", "", text).replace(" ", "")
    return next((key for key in profiles if norm(key) == norm(name)), name)


async def consume_stream(local, path, entry, **kwargs):
    complete = None
    async with local.stream("POST", WORKBENCH + path, **kwargs) as response:
        if response.status_code != 200:
            raise SmartClassError(f"文件转写服务返回 HTTP {response.status_code}")
        async for line in response.aiter_lines():
            if stop_event.is_set():
                raise SmartClassError("已停止，重新选择此课次可从已保存位置继续")
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("session"):
                entry["record_id"] = event["session"]["id"]
                save_imports()
            if event.get("type") == "error":
                raise SmartClassError("文件转写中断，已完成段落保留；重新勾选课次可继续")
            if event.get("stage"):
                entry.update(message=event["stage"], progress=event.get("progress", entry.get("progress", 0)))
                status["message"] = f"{entry['title']}：{event['stage']}"
            if event.get("type") == "complete":
                complete = event["session"]
    if not complete:
        raise SmartClassError("转写连接已中断，可重新勾选课次继续")
    return complete


async def import_stream(local, group, config, entry):
    """The decoder blocks only on durable upload, while the main server runs ASR."""
    headers = {"X-QwenASR-Client": "smartclass"}
    response = await local.post(WORKBENCH + "/api/course-streams", json={
        "key": group["id"], "duration": group["duration"], "config": config,
    }, headers=headers)
    if response.status_code == 404:
        raise SmartClassError("请重启语音工作台以启用分段下载转写")
    if response.status_code != 200:
        raise SmartClassError("分段转写任务创建失败，请检查语音工作台")
    state = response.json()
    entry.update(record_id=state["id"], directory=state["directory"])
    save_imports()
    ident = state["id"]
    if state["status"] == "completed":
        return state
    local_stop = threading.Event()
    downloading = None
    next_index = state["next_index"]
    loop = asyncio.get_running_loop()

    async def send(raw, cursor):
        nonlocal next_index
        for attempt in range(3):
            try:
                r = await local.put(WORKBENCH + f"/api/course-streams/{ident}/chunks/{next_index}",
                    content=raw, headers={**headers, "Content-Type": "application/octet-stream", "X-Course-Cursor": json.dumps(cursor)})
                if r.status_code != 200:
                    raise SmartClassError("转写服务暂停接收音频；已保存分段可继续")
                next_index += 1
                return
            except httpx.HTTPError:
                if attempt == 2:
                    raise SmartClassError("本机转写连接中断，已接收分段可继续") from None
                await asyncio.sleep(1)

    def on_chunk(raw, cursor):
        future = asyncio.run_coroutine_threadsafe(send(raw, cursor), loop)
        # Short waits make cancellation responsive without losing an acknowledged block.
        from concurrent.futures import TimeoutError
        while True:
            try:
                return future.result(timeout=.5)
            except TimeoutError:
                if stop_event.is_set() or local_stop.is_set():
                    future.cancel()
                    raise SmartClassError("下载已停止，已接收音频与转写保留")

    try:
        if not state.get("download_complete"):
            async with platform_lock:
                sources = await asyncio.to_thread(client.audio_sources, group)
            private_json(Path(state["directory"]) / "课程来源.json", {
                "title": group["title"], "periods": group["periods"],
                "sources": [{k:v for k,v in source.items() if k != "url"} for source in sources],
            })
            def progress(part, seconds, duration):
                entry["download_detail"] = f"第 {part + 1}/{len(sources)} 节 · {seconds/60:.1f}/{duration/60:.1f} 分钟"
            downloading = asyncio.create_task(asyncio.to_thread(stream_audio, sources, on_chunk, progress, local_stop, state.get("resume")))
        finished_input = bool(state.get("download_complete"))
        while True:
            if stop_event.is_set():
                raise SmartClassError("任务已停止；重新勾选课次可从已下载位置继续")
            if downloading and downloading.done() and not finished_input:
                manifest = await downloading
                source_path = Path(state["directory"]) / "课程来源.json"
                existing = read_json(source_path, {})
                private_json(source_path, {**existing, "completed_sources": manifest})
                r = await local.post(WORKBENCH + f"/api/course-streams/{ident}/finish", json={"count": next_index}, headers=headers)
                r.raise_for_status()
                finished_input = True
            r = await local.get(WORKBENCH + f"/api/course-streams/{ident}")
            r.raise_for_status()
            state = r.json()
            entry.update(download_seconds=state["download_seconds"], asr_seconds=state["asr_seconds"],
                         corrected_segments=state["corrected_segments"], segments=state["segments"],
                         ai_errors=state["ai_errors"], progress=round(min(99, 100 * state["asr_seconds"] / max(1,group["duration"])),1),
                         message=f"下载 {state['download_seconds']/60:.1f} 分钟 · 转写 {state['asr_seconds']/60:.1f} 分钟 · 精修 {state['corrected_segments']}/{state['segments']} 段")
            status["message"] = f"{group['title']}：{entry['message']}"
            if state["status"] == "completed":
                return state
            if state["status"] == "interrupted":
                raise SmartClassError(state["error"] or "分段转写中断，可重新勾选课次继续")
            await asyncio.sleep(1)
    finally:
        local_stop.set()
        if downloading and not downloading.done():
            await asyncio.gather(downloading, return_exceptions=True)
        if state.get("status") != "completed":
            try:
                await local.post(WORKBENCH + f"/api/course-streams/{ident}/stop", headers=headers)
            except httpx.HTTPError:
                pass


async def import_group(group: dict, run_options: dict | None = None):
    options = run_options if run_options is not None else globals()["options"]
    ident = group["id"]
    entry = imports.setdefault(ident, {"id": ident, "title": group["title"]})
    entry.update(state="running", message="准备回放", progress=0)
    save_imports()
    directory = STATE_DIR / "audio" / ident
    audio_path = directory / "joined.flac"
    manifest_path = directory / "source.json"
    filename = f"smartclass_{ident}.flac"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15), trust_env=False) as local:
        library_response = await local.get(WORKBENCH + "/api/library")
        if library_response.status_code != 200:
            raise SmartClassError("语音工作台未启动，请先启动 QwenASR")
        library = library_response.json()
        records = library.get("records", [])
        record = next((r for r in records if r.get("id") == entry.get("record_id") or r.get("source_filename") == filename), None)
        if record and record.get("status") == "completed" and (entry.get("summary_generated") or not options.get("generate_summary", True)):
            entry.update(state="completed", message="课次已经归档，已跳过重复处理", progress=100)
            save_imports()
            return
        category = category_for(group["course_name"], library.get("category_profiles", {}))
        profile = library.get("category_profiles", {}).get(category, {})
        config = {"title": group["title"], "category": category, "language": "", "translation": "off",
                  "ai_correction": options.get("ai_correction", True), "context": profile.get("asr_prompt", group["course_name"]),
                  "terminology": profile.get("terminology", []), "asr_params": profile.get("asr_params", {}),
                  "ai": {"summary_provider": options.get("summary_provider", {}),
                         "correction_provider": options.get("correction_provider") or options.get("summary_provider", {}),
                         "system_prompt": profile.get("ai_system_prompt", "")}}
        if group["duration"] > 7200:
            raise SmartClassError("两节回放合计超过 2 小时，超出现有文件转写上限")
        if options.get("generate_summary", True) and not options.get("summary_provider", {}).get("model"):
            raise SmartClassError("请在笔记设置中填写 AI 模型，或从语音工作台的课程回放入口同步设置")
        if record and record.get("status") == "completed":
            complete = record
            entry["record_id"] = record["id"]
        elif (not record and not (audio_path.exists() and manifest_path.exists())) or (record and record.get("course_stream")):
            complete = await import_stream(local, group, config, entry)
        elif record:
            entry.update(message="继续已有转写", record_id=record["id"])
            complete = await consume_stream(local, f"/api/recordings/{record['id']}/resume", entry, json=config)
        else:
            if not audio_path.exists() or not manifest_path.exists():
                directory.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(directory).free < 1024**3:
                    raise SmartClassError("磁盘剩余空间不足 1 GB，请先清理空间")
                async with platform_lock:
                    sources = await asyncio.to_thread(client.audio_sources, group)
                def progress(part, seconds, duration):
                    value = 100 * (part + min(1, seconds / max(duration, 1))) / len(sources)
                    entry.update(message=f"提取第 {part + 1}/{len(sources)} 段音轨 · {seconds / 60:.1f} 分钟", progress=round(value, 1))
                    status["message"] = f"{group['title']}：{entry['message']}"
                manifest = await asyncio.to_thread(extract_audio, sources, audio_path, progress, stop_event)
                private_json(manifest_path, {"title": group["title"], "periods": group["periods"], "sources": manifest})
            entry.update(message="音频拼接完成，正在文件转写", progress=0)
            with audio_path.open("rb") as source:
                complete = await consume_stream(local, "/api/transcribe-file", entry,
                    files={"file": (filename, source, "audio/flac")}, data={"config_json": json.dumps(config)})
        entry.update(record_id=complete["id"], directory=complete["directory"])
        if manifest_path.exists():
            private_json(Path(complete["directory"]) / "课程来源.json", read_json(manifest_path, {}))
        if options.get("generate_summary", True):
            entry.update(message="正在生成整堂课笔记", progress=100)
            save_imports()
            response = await local.post(WORKBENCH + f"/api/recordings/{complete['id']}/summary-jobs", json={
                "config": config, "summary_system_prompt": options.get("summary_system_prompt", ""),
                "abstract_system_prompt": options.get("abstract_system_prompt", ""),
            })
            if response.status_code != 200:
                raise SmartClassError("转写已保存，笔记任务提交失败；重新勾选课次可重试")
            while True:
                if stop_event.is_set():
                    raise SmartClassError("已停止等待；已提交的笔记任务仍可在语音工作台查看")
                r = await local.get(WORKBENCH + f"/api/recordings/{complete['id']}/summary-status")
                r.raise_for_status()
                state = r.json()
                if not state.get("running"):
                    if state.get("status") != "completed":
                        raise SmartClassError("转写已保存，AI 笔记尚未完成；可在语音工作台 AI 任务中查看并重试")
                    break
                await asyncio.sleep(3)
        generated = bool(options.get("generate_summary", True))
        entry.update(state="completed", message="已归档为一份课堂笔记" if generated else "已归档合并音频与逐字稿", progress=100,
                     summary_generated=generated,
                     bundle=WORKBENCH + f"/api/recordings/{complete['id']}/download/bundle",
                     summary=WORKBENCH + f"/api/recordings/{complete['id']}/download/ai-summary" if generated else "")
        save_imports()
        # The workbench owns an AAC copy now; clear this importer-owned temporary audio.
        audio_path.unlink(missing_ok=True)


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=30)


@app.post("/api/import")
async def start_import(data: Selection):
    global job
    check_idle()
    if any(ident not in group_rows for ident in data.ids):
        raise HTTPException(400, "课次列表已更新，请重新选择")
    selected = [group_rows[ident] for ident in dict.fromkeys(data.ids)]
    run_options = json.loads(json.dumps(options))
    stop_event.clear()
    async def run():
        status.update(state="running", message="准备获取选中的课次…")
        for group in selected:
            if stop_event.is_set():
                break
            try:
                await import_group(group, run_options)
            except Exception as exc:
                message = str(exc) if isinstance(exc, SmartClassError) else "网络或本机服务中断，已保存进度，可重试"
                imports.setdefault(group["id"], {"id": group["id"], "title": group["title"]}).update(state="error", message=message)
                save_imports()
        errors = sum(imports.get(g["id"], {}).get("state") != "completed" for g in selected)
        status.update(state="connected", message=f"选中课次已处理，{errors} 项需继续或重试" if errors else "选中课次已全部归档")
    job = asyncio.create_task(run())
    return {"started": True}


@app.post("/api/stop")
async def stop():
    stop_event.set()
    return {"stopping": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766, access_log=False)
