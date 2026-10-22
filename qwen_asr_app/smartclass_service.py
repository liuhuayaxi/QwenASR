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
                        LoginRequired, course_key, current_term, lesson_groups, extract_audio, connection_error)
from .course_download import stream_audio
from .replay_bridge import replay_bridge_router
from .course_import_status import lesson_import_statuses, recording_inventory, video_ids
from .ai_preferences import ai_preferences, PROVIDER_FIELDS

ROOT = Path(__file__).resolve().parent.parent
status = {"state": "idle", "message": "请配置统一身份认证账号", "identity": None, "authenticated": False}
client = SmartClassClient()
job = None
catalog_job = None
last_session_check = 0.0
platform_lock = asyncio.Lock()
download_slots = asyncio.Semaphore(2)
queue_wakeup = asyncio.Event()
PIPELINE_CONCURRENCY = 3
course_rows = {}
group_rows = {}
stop_event = threading.Event()
WORKBENCH = "http://127.0.0.1:" + os.getenv("QWEN_ASR_PORT", "8765")
service_draining = False
PLATFORM_TIMEOUT = 30
platform_workers = set()


async def platform_call(function, *args):
    """Cancel the caller promptly while the worker retains exclusive client access."""
    cancelled = threading.Event()
    deadline = time.monotonic() + PLATFORM_TIMEOUT
    current_client = client
    def call():
        with current_client.operation(cancelled, deadline):
            current_client.check_operation()
            return function(*args)
    async def work():
        async with platform_lock:
            if not cancelled.is_set():
                return await asyncio.to_thread(call)
    worker = asyncio.create_task(work())
    platform_workers.add(worker)
    def finished(task):
        platform_workers.discard(task)
        if not task.cancelled():
            task.exception()
    worker.add_done_callback(finished)
    try:
        return await asyncio.wait_for(asyncio.shield(worker), PLATFORM_TIMEOUT)
    except asyncio.TimeoutError:
        cancelled.set()
        raise SmartClassError('学校响应超时，已结束本次检查，可以重试；已有课程列表已保留') from None
    except asyncio.CancelledError:
        cancelled.set()
        raise


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


_last_import_save = 0.0


def save_imports(*, force=True):
    global _last_import_save
    now = time.monotonic()
    if not force and now - _last_import_save < 2:
        return
    private_json(STATE_DIR / "imports.json", imports)
    _last_import_save = now


PHASE_WEIGHTS = {"download": 25, "asr": 45, "correction": 10, "verification": 5, "summary": 10, "review": 5}


def begin_import_progress(entry, run_options):
    phases = {
        "download": {"label": "获取回放音频", "percent": 0, "detail": "等待开始", "state": "pending"},
        "asr": {"label": "转写设备待确认", "percent": 0, "detail": "等待音频", "state": "pending"},
    }
    if run_options.get("ai_correction", True):
        phases["correction"] = {"label": "首轮文字修正", "percent": 0, "detail": "等待转写", "state": "pending"}
        phases["verification"] = {"label": "独立文字复核", "percent": 0, "detail": "等待首轮修正", "state": "pending"}
    if run_options.get("generate_summary", True):
        phases["summary"] = {"label": "课堂笔记", "percent": 0, "detail": "等待转写完成", "state": "pending"}
        phases["review"] = {"label": "笔记逐页审查", "percent": 0, "detail": "等待排版完成", "state": "pending"}
    for key,old in entry.get('phases',{}).items():
        if key in phases and old.get('state')=='completed':phases[key]={**old,'label':phases[key]['label']}
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
    saved = ai_preferences.read()
    providers = saved.get('function_providers', {})
    if providers.get('summary'):
        options['summary_provider'] = providers['summary']
        options['correction_provider'] = providers.get('correction') or providers['summary']
        options['summary_system_prompt'] = (providers.get('summary') or {}).get('system_prompt') or saved.get('detailed_summary_prompt', '')
        options['abstract_system_prompt'] = saved.get('abstract_prompt', '')
        options['generate_summary'] = True
        options['ai_correction'] = True
        private_json(STATE_DIR / 'options.json', options)
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
    private_json(STATE_DIR / 'options.json', options)


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
        # A note may finish through the task center after the replay runner
        # disconnects. Reconcile the queue too, or resume generates it again.
        for queued in queue_state["items"]:
            if imports.get(queued["group"]["id"], {}).get("state") == "completed":
                queued["state"] = "completed"
        save_queue()


@asynccontextmanager
async def lifespan(app):
    global job
    saved = read_json(STATE_DIR / "credentials.json", {})
    if saved.get("username") and saved.get("password"):
        job = asyncio.create_task(connect(saved["username"], saved["password"], False))
    yield
    stop_event.set()
    queue_wakeup.set()
    if job and not job.done():
        await job
    if catalog_job and not catalog_job.done():
        await catalog_job
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
    if action == "request-stop":
        service_draining = True
        await stop()
        return {"ready": True}
    if action != "prepare-stop":
        raise HTTPException(404, "操作不存在")
    if (job and not job.done()) or (catalog_job and not catalog_job.done()):
        raise HTTPException(409, "课程任务仍在进行，请先暂停队列")
    service_draining = True
    return {"ready": True}


@app.get("/api/status")
async def get_status(course: str = ''):
    groups = [group for group in group_rows.values() if group.get('course_key') == course] if course else []
    from .course_phase_status import lesson_phases
    store=getattr(app.state,'recording_store',None)
    shown=[{**entry,'phases':lesson_phases(entry,store.find(entry.get('record_id','')) if store else None)}
           for entry in imports.values() if entry.get('state') in {'running','queued'}]
    return {**status, "service": "courses", "pid": os.getpid(), "instance": os.getenv("QWEN_ASR_INSTANCE", ""), "credentials_saved": (STATE_DIR / "credentials.json").exists(),
            "term": current_term(), "courses": list(course_rows.values()),
            "imports": shown,
            "options": options, "queue": queue_view(),
            "lesson_course": course, "lesson_statuses": await import_statuses(groups)}


@app.get('/api/resources')
async def get_resources():
    from .asr_resources import course_resources
    entries = [dict(entry, id=ident) for ident, entry in list(imports.items())
               if entry.get('state') in {'running', 'queued'}]
    return {'lessons': await course_resources(entries, getattr(app.state, 'recording_store', None))}


async def import_statuses(groups):
    if not groups:
        return {}
    store = getattr(app.state, 'recording_store', None)
    if store is not None:
        records = recording_inventory(store.list_records())
    else:
        # Compatibility with the standalone course service.
        async with httpx.AsyncClient(timeout=5, trust_env=False) as local:
            response = await local.get(WORKBENCH + '/api/library')
            response.raise_for_status()
            records = response.json().get('records', [])
    return lesson_import_statuses(groups, records, imports, queue_state['items'])


async def connect(username, password, save, replay_only=False):
    status.update(state="connecting", message="正在通过南邮统一身份认证登录…", identity=None,
                  authenticated=False, session_check="checking")
    try:
        identity = await platform_call(client.login, username, password)
        if save:
            private_json(STATE_DIR / "credentials.json", {"username": username, "password": password})
        status.update(state="connected" if replay_only else "syncing", identity=identity, authenticated=True, session_check="ready",
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
        status.update(state="error", message=str(exc), session_check="error")
    except httpx.HTTPError as exc:
        status.update(state="error", message=connection_error(exc, client.last_stage))
    except Exception:
        status.update(state="error", message="学校响应格式发生变化，需要检查适配")


@app.post("/api/connect")
async def connect_account(data: Credentials):
    global job
    if (job and not job.done()) or (catalog_job and not catalog_job.done()):
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
    if (job and not job.done()) or (catalog_job and not catalog_job.done()):
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


async def load_courses(*, verify_session=False):
    def load():
        identity = client.ensure_login() if verify_session else None
        return identity, client.courses()
    identity, rows = await platform_call(load)
    if verify_session:
        status.update(identity=identity, authenticated=True)
    course_rows.clear()
    group_rows.clear()
    for row in rows:
        course_rows[course_key(row)] = {**row, "key": course_key(row)}
    status["course_revision"] = time.time_ns()
    if status.get("state") != "running":
        status.update(state="connected", message=f"本学期 {len(rows)} 条课程记录，选择课程查看回放")


@app.post("/api/ensure-session")
async def ensure_session(data: ReconnectOptions = ReconnectOptions()):
    """Page entry checks authentication and refreshes discovery without resuming imports."""
    global catalog_job, last_session_check
    if ((catalog_job and not catalog_job.done()) or status.get("state") in {"connecting", "syncing"}
            or (status.get("authenticated") and time.monotonic() - last_session_check < 10)):
        return {"started": False}
    last_session_check = time.monotonic()
    async def check():
        running = status.get("state") == "running"
        status["session_check"] = "checking"
        status["authenticated"] = False
        if not running:
            status.update(state="syncing", message="正在检查登录并更新课程…")
        try:
            if data.replay_only:
                identity = await platform_call(client.ensure_login)
                status.update(identity=identity, authenticated=True)
                if not running:
                    status.update(state="connected", message="登录已确认，可以返回学校页面")
            else:
                await load_courses(verify_session=True)
            status["session_check"] = "ready"
        except (SmartClassError, httpx.HTTPError) as exc:
            status["session_check"] = "error"
            if isinstance(exc, LoginRequired):
                status.update(authenticated=False, identity=None)
            message = connection_error(exc, client.last_stage) if isinstance(exc, httpx.HTTPError) else str(exc)
            if not running:
                status.update(state="error", message=message)
        except Exception:
            status["session_check"] = "error"
            if not running:
                status.update(state="error", message="学校响应格式发生变化，需要检查适配")
    catalog_job = asyncio.create_task(check())
    return {"started": True}


@app.post("/api/cancel-connection")
async def cancel_connection():
    global last_session_check
    tasks = [catalog_job] if catalog_job and not catalog_job.done() else []
    if status.get('state') in {'connecting', 'syncing'} and job and not job.done():
        tasks.append(job)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    last_session_check = time.monotonic()
    status['session_check'] = 'cancelled'
    if status.get('state') != 'running':
        status.update(state='connected' if status.get('authenticated') else 'idle',
                      message='已取消登录或课程刷新，已有课程列表已保留')
    return {'cancelled': True}


@app.post("/api/refresh")
async def refresh_courses():
    global catalog_job
    if status.get("state") in {"connecting", "syncing"}:
        raise HTTPException(409, "正在连接或刷新课程，请稍后重试")
    if catalog_job and not catalog_job.done():
        return {"started": False}
    if not status.get("identity"):
        raise HTTPException(400, "请先登录")
    async def refresh():
        if status.get("state") != "running":
            status.update(state="syncing", message="正在刷新本学期课程…")
        try:
            await load_courses(verify_session=True)
        except SmartClassError as exc:
            if status.get("state") != "running":
                status.update(state="error", message=str(exc))
        except Exception:
            if status.get("state") != "running":
                status.update(state="error", message="刷新课程失败，请重新连接账号后重试")
    catalog_job = asyncio.create_task(refresh())
    return {"started": True}


@app.get("/api/lessons")
async def lessons(course: str):
    if course not in course_rows:
        raise HTTPException(404, "请先选择本学期课程")
    try:
        def load():
            return client.ensure_login(), client.videos(course_rows[course])
        identity, videos = await platform_call(load)
        status.update(identity=identity, authenticated=True)
        groups = lesson_groups(videos, course_rows[course])
        course_rows[course]["VideoTotal"] = len(videos)
        group_rows.update({g["id"]: g for g in groups})
        states = await import_statuses(groups)
        return {"groups": [{**group, 'import_status': states[group['id']]} for group in groups]}
    except (SmartClassError, httpx.HTTPError, ValueError):
        raise HTTPException(502, "获取回放失败，请刷新登录状态后重试")


@app.post("/api/options")
async def set_options(request: Request):
    data = await request.json()
    for name in ("summary_provider", "correction_provider"):
        provider = data.get(name) or {}
        if provider:
            options[name] = {k: v for k, v in provider.items() if k in PROVIDER_FIELDS}
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
    store=getattr(app.state,'recording_store',None)
    cached=store.find(entry.get('record_id','')) if store else None
    saved=cached.metadata.get('course_stream',{}) if cached else {}
    cached_sources=saved.get('sources') or []
    local_complete=bool(saved.get('input_complete') and cached_sources
        and (group['id'] in saved.get('keys',[]) or cached.metadata.get('source_filename')==f"smartclass_{group['id']}.flac")
        and {str(s['video_id']) for s in cached_sources}==set(video_ids(group)))
    if local_complete:
        # Creation verifies every retained chunk hash before starting the AI
        # pipeline. Completed input needs no fresh campus URL or download.
        sources=cached_sources
        complete_import_phase(entry,'download','完整音频已在本地，核对分片后继续')
    else:
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
    if local_complete and not state.get('download_complete'):
        raise SmartClassError('本地音频完成标记发生变化，已保留分片，请核对后继续')
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
            resume = state.get("resume")
            async def download():
                update_import_phase(entry, "download", 0, "等待下载通道", "pending")
                async with download_slots:
                    if stop_event.is_set() or local_stop.is_set():
                        raise SmartClassError("下载已暂停，已保存进度可继续")
                    update_import_phase(entry, "download", 1, "正在并行获取回放音频")
                    return await asyncio.to_thread(stream_audio, sources, on_chunk, progress, local_stop, resume)
            downloading = asyncio.create_task(download())
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
                f"{state['download_seconds']/60:.1f}/{expected/60:.1f} 分钟" if state["download_seconds"] or finished_input else entry["phases"]["download"]["detail"],
                "completed" if state.get("download_complete") else entry["phases"]["download"]["state"],
            )
            update_import_phase(
                entry,
                "asr",
                100 if state.get('asr_complete') or state["status"] == "completed" else asr_percent,
                '语音转写已保存 · '+str(state['segments'])+' 段' if state.get('asr_complete') else f"{state['asr_seconds']/60:.1f}/{expected/60:.1f} 分钟",
                "completed" if state.get('asr_complete') or state["status"] == "completed" else "running",
            )
            if "correction" in entry.get("phases", {}):
                segment_count = int(state["segments"] or 0)
                refinement=state.get('refinement') or {}
                saved=refinement.get('saved_segments',state['corrected_segments'])
                correction_done=state['status']=='completed' or bool(state.get('asr_complete') and refinement and not refinement.get('remaining_segments'))
                correction_percent = 100 if correction_done else 100 * saved / max(1, segment_count)
                update_import_phase(
                    entry,
                    "correction",
                    correction_percent,
                    f"已保存 {saved}/{segment_count} 段",
                    "completed" if correction_done else ("running" if segment_count else "pending"),
                )
                verification=state.get('verification') or {}
                if verification:
                    total=int(verification.get('total') or 0);done=int(verification.get('completed') or 0)
                    update_import_phase(entry,'verification',100*done/max(1,total),
                        f"已复核 {done}/{total} 条修改"+("；继续未完成复核" if verification.get('status')=='failed' else ''),
                        'error' if verification.get('status')=='failed' else verification.get('status','pending'))
            status["message"] = f"{group['title']}：{entry['message']}"
            # The workbench task feed reads this checkpoint in another process.
            save_imports(force=False)
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
    entry.update(state="running", message="准备回放", progress=0, video_ids=video_ids(group))
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
                  "course_time": {'start': group['start'], 'end': group['stop']},
                  "generate_summary": options.get("generate_summary", True),
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
            # The library list intentionally omits the directory and large
            # transcript. A resumed, already-transcribed lesson still needs
            # its full identity to publish the note; never transcribe again.
            response = await local.get(WORKBENCH + f"/api/recordings/{record['id']}")
            response.raise_for_status()
            complete = response.json().get("session") or {}
            if complete.get("id") != record["id"] or not complete.get("directory"):
                raise SmartClassError("原转写已保留，但无法核对课堂保存位置；请刷新后继续")
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
        complete_import_phase(entry, "correction", "首轮文字修正已保存")
        complete_import_phase(entry, "verification", "独立文字复核已完成")
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
                    unit = state.get('progress_unit') if state.get('progress_unit') in {'页', '批'} else '个分块'
                    summary_detail += f" · {completed_parts}/{total_parts} {unit}"
                if re.search('独立审查|逐页审查|修订',summary_detail):
                    complete_import_phase(entry,'summary','笔记已生成并排版')
                    update_import_phase(entry,'review',summary_percent,summary_detail)
                else:
                    update_import_phase(entry, "summary", summary_percent, summary_detail)
                entry["message"] = summary_detail
                status["message"] = f"{group['title']}：{summary_detail}"
                save_imports(force=False)
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


@app.post('/api/queue/remove')
async def remove_queue_items(data: Selection):
    """Remove selected idle queue entries, keeping lesson files and import history."""
    wanted = set(data.ids)
    selected = [item for item in queue_state['items'] if item['group']['id'] in wanted]
    if any(item.get('state') in {'queued', 'running'} for item in selected):
        raise HTTPException(409, '课次仍在运行或排队，请先暂停该任务')
    if not selected:
        return {'removed': [], 'queue': queue_view()}
    receipt_path = STATE_DIR / 'removed-queue-items.json'
    receipts = read_json(receipt_path, {'items': []})
    receipts['items'].append({'removed_at': time.time(), 'entries': selected})
    private_json(receipt_path, receipts)
    previous = queue_state['items']
    queue_state['items'] = [item for item in previous if item['group']['id'] not in wanted]
    try:
        save_queue()
    except BaseException:
        queue_state['items'] = previous
        raise
    return {'removed': [item['group']['id'] for item in selected], 'queue': queue_view()}


async def run_queue(*, retry_interrupted=True):
    """Keep a bounded pipeline alive, including selections appended during a run."""
    active = {}
    for item in queue_state["items"]:
        if retry_interrupted and item["state"] == "interrupted":
            item["state"] = "queued"

    async def execute(item):
        group = item["group"]
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
        finally:
            save_queue()
            queue_wakeup.set()

    status.update(state="running", message="准备继续已提交的课次…")
    try:
        while True:
            queue_wakeup.clear()
            for task in tuple(active):
                if task.done():
                    active.pop(task)
                    await task
            stopping = stop_event.is_set() or queue_state.get("paused")
            if not stopping:
                admitted = False
                active_videos = {video for item in active.values() for video in video_ids(item["group"])}
                for item in queue_state["items"]:
                    if len(active) >= PIPELINE_CONCURRENCY:
                        break
                    if item["state"] != "queued":
                        continue
                    sources = set(video_ids(item["group"]))
                    if sources & active_videos:
                        continue
                    item["state"] = "running"
                    active[asyncio.create_task(execute(item))] = item
                    active_videos.update(sources)
                    admitted = True
                if admitted:
                    save_queue()
            if not active:
                break
            pending = sum(item["state"] == "queued" for item in queue_state["items"])
            status.update(state="running", message="正在暂停队列，保存各课次进度…" if stopping else
                          f"流水线处理中：{len(active)} 个课次 · 等待 {pending} 个；可以继续添加课次")
            # Wake on appended work or completion. A short timeout also observes stop.
            try:
                await asyncio.wait_for(queue_wakeup.wait(), .5)
            except TimeoutError:
                pass
    finally:
        if active:
            stop_event.set()
            await asyncio.gather(*active, return_exceptions=True)
    remaining = queue_view()["remaining"]
    status.update(state="connected", message=f"还有 {remaining} 项已保存，可点击继续队列" if remaining else "选中课次已全部归档")


@app.post("/api/import")
async def start_import(data: Selection):
    global job
    running = bool(job and not job.done())
    if running and (status.get("state") != "running" or stop_event.is_set() or queue_state.get("paused")):
        raise HTTPException(409, "正在连接或暂停队列，请稍后再添加课次")
    if not status.get("identity"):
        raise HTTPException(400, "请先登录")
    if any(ident not in group_rows for ident in data.ids):
        raise HTTPException(400, "课次列表已更新，请重新选择")
    added = []
    for ident in dict.fromkeys(data.ids):
        item = next((i for i in queue_state["items"] if i["group"]["id"] == ident), None)
        # HTTP retries and overlapping selections must not reset a live task.
        if item and item["state"] in {"queued", "running"}:
            continue
        if item is None:
            item = {"group": json.loads(json.dumps(group_rows[ident]))}
            queue_state["items"].append(item)
        else:
            item["group"] = json.loads(json.dumps(group_rows[ident]))
        item.update(state="queued", options=json.loads(json.dumps(options)))
        entry = imports.setdefault(ident, {"id": ident, "title": group_rows[ident]["title"]})
        entry.update(state="queued", message="已加入入库队列", video_ids=video_ids(group_rows[ident]))
        added.append(ident)
    if not added:
        return {"started": False, "already_queued": True, "added": []}
    queue_state["paused"] = False
    save_queue()  # Persist acceptance before returning to the browser.
    save_imports()
    stop_event.clear()
    status.update(state="running", message="选中课次已加入队列")
    queue_wakeup.set()
    if not running:
        job = asyncio.create_task(run_queue(retry_interrupted=False))
    return {"started": True, "added": added}


@app.post("/api/resume")
async def resume_queue():
    global job
    check_idle()
    for item in queue_state["items"]:
        if item["state"] != "completed":
            item["state"] = "queued"
    queue_state["paused"] = False
    save_queue()
    stop_event.clear()
    status.update(state="running", message="正在继续已保存队列")
    async def resume_saved():
        # A service restart clears memory, not the saved account. Restore it
        # here as well as on startup, without asking the user to log in again.
        if not status.get("identity") or not status.get("authenticated", True):
            status.update(session_check="checking", message="正在使用已保存账号恢复课程连接…")
            try:
                async with platform_lock:
                    identity = await asyncio.to_thread(client.ensure_login)
                status.update(identity=identity, authenticated=True, session_check="ready")
                status.pop("connection_warning", None)
            except (SmartClassError, httpx.HTTPError) as exc:
                message = connection_error(exc, client.last_stage) if isinstance(exc, httpx.HTTPError) else str(exc)
                status.update(authenticated=False, session_check="error", connection_warning=message)
                # Already saved audio/transcripts do not depend on the portal.
                # Any remaining downloads retain their own truthful failure.
        if not stop_event.is_set() and not queue_state.get("paused"):
            try:
                await reconcile_imports()
            except (httpx.HTTPError, ValueError):
                pass
            await run_queue()
    job = asyncio.create_task(resume_saved())
    return {"started": True}


@app.post("/api/stop")
async def stop():
    queue_state["paused"] = True
    for item in queue_state["items"]:
        if item["state"] in {"queued", "running", "interrupted"}:
            if item["state"] != "running":
                entry = imports.get(item["group"]["id"])
                if entry:
                    entry.update(state="paused", message="队列已暂停，可继续已保存进度")
            item["state"] = "paused"
    save_queue()
    save_imports()
    stop_event.set()
    queue_wakeup.set()
    return {"stopping": True, "paused": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("QWEN_ASR_PORT", "8765")), access_log=False)
