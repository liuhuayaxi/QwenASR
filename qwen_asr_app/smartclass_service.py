"""Independent local course sync service, so a live recording need not restart."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware

from .smartclass import (STATE_DIR, SmartClassClient, SmartClassError, private_json,
                        course_key, current_term, lesson_groups, extract_audio, connection_error)
from .course_download import stream_audio
from .replay_bridge import replay_bridge_router

ROOT = Path(__file__).resolve().parent.parent
status = {"state": "idle", "message": "请配置统一身份认证账号", "identity": None, "authenticated": False}
client = SmartClassClient()
job = None
platform_lock = asyncio.Lock()
course_rows = {}
group_rows = {}
stop_event = threading.Event()
WORKBENCH = "http://127.0.0.1:" + os.getenv("QWEN_ASR_PORT", "8765")
service_draining = False


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


imports = read_json(STATE_DIR / "imports.json", {})
options = read_json(STATE_DIR / "options.json", {})
queue_state = read_json(STATE_DIR / "queue.json", {"items": [], "paused": False})
for item in queue_state["items"]:
    if item.get("state") == "running":
        item["state"] = "paused" if queue_state.get("paused") else "interrupted"


def save_queue():
    private_json(STATE_DIR / "queue.json", queue_state)


def queue_view():
    return {"paused": queue_state.get("paused", False),
            "remaining": sum(i.get("state") != "completed" for i in queue_state["items"]),
            "items": [{"id": i["group"]["id"], "title": i["group"]["title"], "state": i["state"]} for i in queue_state["items"]]}

for item in imports.values():
    if item.get("state") not in {"completed", "error", "interrupted"}:
        item.update(state="interrupted", message="服务已重启，可勾选原课次继续")
        for phase in item.get("phases", {}).values():
            if phase.get("state") == "running":
                phase.update(state="paused", detail="服务已重启，可继续已保存进度")


def save_imports():
    private_json(STATE_DIR / "imports.json", imports)


PHASE_WEIGHTS = {"download": 25, "asr": 45, "correction": 15, "summary": 15}


def begin_import_progress(entry, run_options):
    phases = {
        "download": {"label": "获取回放音频", "percent": 0, "detail": "等待开始", "state": "pending"},
        "asr": {"label": "本地语音转写", "percent": 0, "detail": "等待音频", "state": "pending"},
    }
    if run_options.get("ai_correction", True):
        phases["correction"] = {"label": "AI 精修", "percent": 0, "detail": "等待转写", "state": "pending"}
    if run_options.get("generate_summary", True):
        phases["summary"] = {"label": "课堂笔记", "percent": 0, "detail": "等待转写完成", "state": "pending"}
    entry["phases"] = phases
    entry["progress"] = 0


def update_import_phase(entry, key, percent, detail, phase_state="running"):
    phase = entry.setdefault("phases", {}).get(key)
    if phase is None:
        return
    phase.update(
        percent=round(max(0, min(100, float(percent))), 1),
        detail=str(detail or ""),
        state=phase_state,
    )
    active_weights = {name: PHASE_WEIGHTS[name] for name in entry["phases"]}
    total_weight = sum(active_weights.values()) or 1
    entry["progress"] = round(
        sum(entry["phases"][name]["percent"] * weight for name, weight in active_weights.items()) / total_weight,
        1,
    )


def complete_import_phase(entry, key, detail="已完成"):
    update_import_phase(entry, key, 100, detail, "completed")


def complete_import_progress(entry):
    for key in tuple(entry.get("phases", {})):
        complete_import_phase(entry, key)
    entry["progress"] = 100


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
            complete_import_progress(item)
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
app.include_router(replay_bridge_router(client, platform_lock))
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
                   allow_methods=["POST"], allow_headers=["Content-Type", "X-QwenASR-Client"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    path = request.url.path
    is_service_control = "/api/service/" in path
    if service_draining and request.method not in {"GET", "HEAD"} and not is_service_control:
        return JSONResponse({"detail": "服务正在安全退出，请重新启动后继续"}, status_code=503)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        origin = request.headers.get("origin")
        bridge = path.endswith("/api/options") and origin in {"http://127.0.0.1:8765", "http://localhost:8765"}
        if (origin != expected and not bridge) or request.headers.get("x-qwenasr-client") != "smartclass":
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
    replay_only: bool = False


class ReconnectOptions(BaseModel):
    replay_only: bool = False


@app.get("/", response_class=HTMLResponse)
async def index():
    return (ROOT / "static" / "smartclass.html").read_text(encoding="utf-8")


@app.post("/api/service/{action}")
async def service_stop_guard(action: str, request: Request):
    global service_draining
    token = os.getenv("QWEN_ASR_INSTANCE")
    if not token or request.headers.get("X-QwenASR-Control") != token:
        raise HTTPException(403, "请使用本机停止服务启动器")
    if action == "cancel-stop":
        service_draining = False
        return {"ready": False}
    if action != "prepare-stop":
        raise HTTPException(404, "操作不存在")
    if job and not job.done():
        raise HTTPException(409, "课程任务仍在进行，请先暂停队列")
    service_draining = True
    return {"ready": True}


@app.get("/api/status")
async def get_status():
    return {**status, "service": "courses", "pid": os.getpid(), "instance": os.getenv("QWEN_ASR_INSTANCE", ""), "credentials_saved": (STATE_DIR / "credentials.json").exists(),
            "term": current_term(), "courses": list(course_rows.values()),
            "imports": list(imports.values()), "options": options, "queue": queue_view()}


async def connect(username, password, save, replay_only=False):
    if not replay_only:
        course_rows.clear()
        group_rows.clear()
    status.update(state="connecting", message="正在通过南邮统一身份认证登录…", identity=None, authenticated=False)
    try:
        async with platform_lock:
            identity = await asyncio.to_thread(client.login, username, password)
        if save:
            private_json(STATE_DIR / "credentials.json", {"username": username, "password": password})
        status.update(state="connected" if replay_only else "syncing", identity=identity, authenticated=True,
                      message="登录成功，可以返回回放" if replay_only else "登录成功，正在获取本学期课程…")
        if replay_only:
            return
        await load_courses()
        try:
            await defaults()
            await reconcile_imports()
        except (httpx.HTTPError, ValueError):
            pass
        if not queue_state.get("paused") and any(i["state"] in {"queued", "interrupted"} for i in queue_state["items"]):
            stop_event.clear()
            await run_queue()
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
    status.update(state="connecting", identity=None, authenticated=False)
    job = asyncio.create_task(connect(data.username.strip(), password, data.save, data.replay_only))
    return {"started": True}


@app.post("/api/reconnect")
async def reconnect(data: ReconnectOptions = ReconnectOptions()):
    global job
    if job and not job.done():
        raise HTTPException(409, "已有任务正在进行")
    try:
        saved = json.loads((STATE_DIR / "credentials.json").read_text())
    except (OSError, ValueError):
        raise HTTPException(400, "尚未保存本机登录配置")
    status.update(state="connecting", identity=None, authenticated=False)
    job = asyncio.create_task(connect(saved["username"], saved["password"], False, data.replay_only))
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
                event_progress = float(event.get("progress", 0) or 0)
                entry["message"] = event["stage"]
                if event_progress <= 68:
                    asr_percent = max(1, min(100, (event_progress - 7) * 100 / 61))
                    update_import_phase(entry, "asr", asr_percent, event["stage"])
                else:
                    complete_import_phase(entry, "asr", "语音转写已完成")
                    correction_percent = min(99, (event_progress - 68) * 100 / 27)
                    update_import_phase(entry, "correction", correction_percent, event["stage"])
                status["message"] = f"{entry['title']}：{event['stage']}"
            if event.get("type") == "complete":
                complete = event["session"]
                complete_import_phase(entry, "asr", "语音转写已完成")
                complete_import_phase(entry, "correction", "AI 精修已完成")
    if not complete:
        raise SmartClassError("转写连接已中断，可重新勾选课次继续")
    return complete


async def import_stream(local, group, config, entry):
    """The decoder blocks only on durable upload, while the main server runs ASR."""
    headers = {"X-QwenASR-Client": "smartclass"}
    update_import_phase(entry, "download", 1, "正在获取回放音轨地址")
    async with platform_lock:
        sources = await asyncio.to_thread(client.audio_sources, group)
    response = await local.post(WORKBENCH + "/api/course-streams", json={
        "key": group["id"], "duration": group["duration"], "config": config,
        "sources": [{k: v for k, v in source.items() if k != "url"} for source in sources],
    }, headers=headers)
    if response.status_code == 404:
        raise SmartClassError("请重启语音工作台以启用分段下载转写")
    if response.status_code != 200:
        detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
        raise SmartClassError(detail if isinstance(detail, str) else "分段转写任务创建失败，请检查语音工作台")
    state = response.json()
    entry.update(record_id=state["id"], directory=state["directory"])
    save_imports()
    ident = state["id"]
    if state["status"] == "completed":
        complete_import_phase(entry, "download", "回放音频已获取")
        complete_import_phase(entry, "asr", "语音转写已完成")
        complete_import_phase(entry, "correction", "AI 精修已完成")
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
            source_path = Path(state["directory"]) / "课程来源.json"
            previous = read_json(source_path, {})
            # Legacy imports store decoded lengths in sources, not completed_sources.
            completed = {(str(s["video_id"]), str(s["segment_id"])): s
                         for s in previous.get("sources", []) if s.get("duration_seconds")}
            completed.update({(str(s["video_id"]), str(s["segment_id"])): s
                              for s in previous.get("completed_sources", [])})
            private_json(source_path, {**previous,
                "title": group["title"], "periods": group["periods"],
                "sources": [{k:v for k,v in source.items() if k != "url"} for source in sources],
                "completed_sources": list(completed.values()),
            })
            def progress(part, seconds, duration):
                entry["download_detail"] = f"第 {part + 1}/{len(sources)} 节 · {seconds/60:.1f}/{duration/60:.1f} 分钟"
                value = 100 * (part + min(1, seconds / max(duration, 1))) / max(1, len(sources))
                update_import_phase(entry, "download", value, entry["download_detail"])
            downloading = asyncio.create_task(asyncio.to_thread(stream_audio, sources, on_chunk, progress, local_stop, state.get("resume")))
        finished_input = bool(state.get("download_complete"))
        while True:
            if stop_event.is_set():
                raise SmartClassError("任务已停止；重新勾选课次可从已下载位置继续")
            if downloading and downloading.done() and not finished_input:
                manifest = await downloading
                source_path = Path(state["directory"]) / "课程来源.json"
                existing = read_json(source_path, {})
                completed = {(str(s["video_id"]), str(s["segment_id"])): s
                             for s in existing.get("completed_sources", [])}
                completed.update({(str(s["video_id"]), str(s["segment_id"])): s for s in manifest})
                private_json(source_path, {**existing, "completed_sources": list(completed.values())})
                r = await local.post(WORKBENCH + f"/api/course-streams/{ident}/finish", json={"count": next_index}, headers=headers)
                r.raise_for_status()
                finished_input = True
            r = await local.get(WORKBENCH + f"/api/course-streams/{ident}")
            r.raise_for_status()
            state = r.json()
            expected = max(1, float(state.get("expected_seconds") or group["duration"]))
            download_percent = min(100, 100 * state["download_seconds"] / expected)
            asr_percent = min(100, 100 * state["asr_seconds"] / expected)
            progress_message = f"获取 {download_percent:.0f}% · 转写 {asr_percent:.0f}%"
            if "correction" in entry.get("phases", {}):
                progress_message += f" · 精修 {state['corrected_segments']}/{state['segments']} 段"
            entry.update(download_seconds=state["download_seconds"], asr_seconds=state["asr_seconds"],
                         corrected_segments=state["corrected_segments"], segments=state["segments"],
                         ai_errors=state["ai_errors"], metrics=state.get("metrics", {}),
                         message=progress_message)
            update_import_phase(
                entry,
                "download",
                100 if state.get("download_complete") else download_percent,
                f"{state['download_seconds']/60:.1f}/{expected/60:.1f} 分钟",
                "completed" if state.get("download_complete") else "running",
            )
            update_import_phase(
                entry,
                "asr",
                100 if state["status"] == "completed" else asr_percent,
                f"{state['asr_seconds']/60:.1f}/{expected/60:.1f} 分钟 · {state.get('stage') or '正在转写'}",
                "completed" if state["status"] == "completed" else "running",
            )
            if "correction" in entry.get("phases", {}):
                segment_count = int(state["segments"] or 0)
                correction_percent = 100 if state["status"] == "completed" else 100 * state["corrected_segments"] / max(1, segment_count)
                update_import_phase(
                    entry,
                    "correction",
                    correction_percent,
                    f"{state['corrected_segments']}/{segment_count} 段",
                    "completed" if state["status"] == "completed" else ("running" if segment_count else "pending"),
                )
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
                await local.post(WORKBENCH + f"/api/course-streams/{ident}/stop", headers=headers, json={"paused": bool(queue_state.get("paused"))})
            except httpx.HTTPError:
                pass


async def import_group(group: dict, run_options: dict | None = None):
    options = run_options if run_options is not None else globals()["options"]
    ident = group["id"]
    entry = imports.setdefault(ident, {"id": ident, "title": group["title"]})
    entry.update(state="running", message="准备回放", progress=0)
    begin_import_progress(entry, options)
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
            complete_import_progress(entry)
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
            complete_import_phase(entry, "download", "回放音频已获取")
            complete_import_phase(entry, "asr", "语音转写已完成")
            complete_import_phase(entry, "correction", "AI 精修已完成")
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
                    detail = f"第 {part + 1}/{len(sources)} 节 · 已获取 {seconds / 60:.1f} 分钟"
                    entry.update(message=f"获取回放音频 · {detail}")
                    update_import_phase(entry, "download", value, detail)
                    status["message"] = f"{group['title']}：{entry['message']}"
                manifest = await asyncio.to_thread(extract_audio, sources, audio_path, progress, stop_event)
                private_json(manifest_path, {"title": group["title"], "periods": group["periods"], "sources": manifest})
            complete_import_phase(entry, "download", "回放音频已获取并合并")
            update_import_phase(entry, "asr", 1, "音频已就绪，正在启动本地转写")
            entry.update(message="音频拼接完成，正在文件转写")
            with audio_path.open("rb") as source:
                complete = await consume_stream(local, "/api/transcribe-file", entry,
                    files={"file": (filename, source, "audio/flac")}, data={"config_json": json.dumps(config)})
        entry.update(record_id=complete["id"], directory=complete["directory"])
        complete_import_phase(entry, "download", "回放音频已获取")
        complete_import_phase(entry, "asr", "语音转写已完成")
        complete_import_phase(entry, "correction", "AI 精修已完成")
        if manifest_path.exists():
            private_json(Path(complete["directory"]) / "课程来源.json", read_json(manifest_path, {}))
        if options.get("generate_summary", True):
            update_import_phase(entry, "summary", 1, "正在提交课堂笔记任务")
            entry.update(message="正在生成整堂课笔记")
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
                    complete_import_phase(entry, "summary", "课堂笔记已生成")
                    break
                completed_parts = int(state.get("completed_parts") or 0)
                total_parts = int(state.get("total_parts") or 0)
                summary_percent = 5 if not total_parts else min(95, 100 * completed_parts / total_parts)
                summary_detail = str(state.get("phase") or "正在生成课堂笔记")
                if total_parts:
                    summary_detail += f" · {completed_parts}/{total_parts} 个分块"
                update_import_phase(entry, "summary", summary_percent, summary_detail)
                entry["message"] = summary_detail
                status["message"] = f"{group['title']}：{summary_detail}"
                await asyncio.sleep(3)
        generated = bool(options.get("generate_summary", True))
        complete_import_progress(entry)
        entry.update(state="completed", message="已归档为一份课堂笔记" if generated else "已归档合并音频与逐字稿", progress=100,
                     summary_generated=generated,
                     bundle=WORKBENCH + f"/api/recordings/{complete['id']}/download/bundle",
                     summary=WORKBENCH + f"/api/recordings/{complete['id']}/download/ai-summary" if generated else "")
        save_imports()
        # The workbench owns an AAC copy now; clear this importer-owned temporary audio.
        audio_path.unlink(missing_ok=True)


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=30)


async def run_queue():
    status.update(state="running", message="准备继续已提交的课次…")
    for item in queue_state["items"]:
        if stop_event.is_set() or queue_state.get("paused"):
            break
        if item["state"] not in {"queued", "interrupted"}:
            continue
        group = item["group"]
        item["state"] = "running"
        save_queue()
        try:
            await import_group(group, item["options"])
            item["state"] = "completed"
        except (Exception, asyncio.CancelledError) as exc:
            item["state"] = "paused" if queue_state.get("paused") else "interrupted"
            message = str(exc) if isinstance(exc, SmartClassError) else "网络或本机服务中断，已保存进度，可继续"
            import_entry = imports.setdefault(group["id"], {"id": group["id"], "title": group["title"]})
            phase_state = "paused" if queue_state.get("paused") else "error"
            for phase in import_entry.get("phases", {}).values():
                if phase.get("state") == "running":
                    phase.update(state=phase_state, detail=message)
            import_entry.update(state=item["state"], message=message)
            save_imports()
            if isinstance(exc, asyncio.CancelledError):
                save_queue()
                raise
        save_queue()
    remaining = queue_view()["remaining"]
    status.update(state="connected", message=f"还有 {remaining} 项已保存，可点击继续队列" if remaining else "选中课次已全部归档")


@app.post("/api/import")
async def start_import(data: Selection):
    global job
    # A repeated click/replayed HTTP response is the same accepted selection.
    if job and not job.done() and status.get("state") == "running":
        accepted = {i["group"]["id"] for i in queue_state["items"] if i["state"] in {"queued", "running", "completed"}}
        if set(data.ids) <= accepted:
            return {"started": False, "already_queued": True}
    check_idle()
    if any(ident not in group_rows for ident in data.ids):
        raise HTTPException(400, "课次列表已更新，请重新选择")
    # Keep unfinished unselected items paused when starting a new selection.
    for item in queue_state["items"]:
        if item["state"] != "completed":
            item["state"] = "paused"
    queue_state["items"] = [i for i in queue_state["items"] if i["state"] != "completed" or i["group"]["id"] in data.ids]
    for ident in dict.fromkeys(data.ids):
        item = next((i for i in queue_state["items"] if i["group"]["id"] == ident), None)
        if item is None:
            item = {"group": json.loads(json.dumps(group_rows[ident]))}
            queue_state["items"].append(item)
        item.update(state="queued", options=json.loads(json.dumps(options)))
    queue_state["paused"] = False
    save_queue()  # Persist acceptance before returning to the browser.
    stop_event.clear()
    status.update(state="running", message="选中课次已加入队列")
    job = asyncio.create_task(run_queue())
    return {"started": True}


@app.post("/api/resume")
async def resume_queue():
    global job
    check_idle()
    if not status.get("identity"):
        raise HTTPException(400, "请先重新连接账号，再继续已保存队列")
    for item in queue_state["items"]:
        if item["state"] != "completed":
            item["state"] = "queued"
    queue_state["paused"] = False
    save_queue()
    stop_event.clear()
    status.update(state="running", message="正在继续已保存队列")
    job = asyncio.create_task(run_queue())
    return {"started": True}


@app.post("/api/stop")
async def stop():
    queue_state["paused"] = True
    for item in queue_state["items"]:
        if item["state"] in {"queued", "running", "interrupted"}:
            item["state"] = "paused"
    save_queue()
    stop_event.set()
    return {"stopping": True, "paused": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("QWEN_ASR_PORT", "8765")), access_log=False)
