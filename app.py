from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Request, Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from qwen_asr_app.batch_pipeline import SegmentCorrectionPipeline
from qwen_asr_app.ai_secrets import ai_secret_store
from qwen_asr_app.ai_preferences import ai_preferences
from qwen_asr_app.audio import (convert_audio_to_aac, decode_audio_file,
                                 split_audio_to_aac, split_audio_to_aac_lossless,
                                 vad_split_ranges)
from qwen_asr_app.batch_ai import (
    build_note_summary,
    correct_batch_segment,
    global_rewrite_events,
    regenerate_note_abstract,
    segment_ai_complete,
)
from qwen_asr_app.context_budget import (
    DEFAULT_CONTEXT_WINDOW,
    chunk_text_blocks,
    context_safe_source_budget,
    estimate_tokens,
)
from qwen_asr_app.ai import (
    concurrency_for,
    close_ai_clients,
    context_window_for,
    generate_recording_title,
    output_limit_for,
    provider_for_mode,
    provider_transport,
    uses_agy_gemini,
    agy_rewrite_source_budget,
    polish_and_translate,
    polish_timestamped_segments,
    polish_timestamped_segments_resilient,
    summarize_transcript,
    test_ai_connection,
)
from qwen_asr_app.note_content import compose_push, compose_push_part, summary_from_archive, transcript_blocks
from qwen_asr_app.live_session import TranscriptionSession
from qwen_asr_app.ideashell_sync import ideashell_manager
from qwen_asr_app.ideashell_attachments import MAX_AUDIO_BYTES, audio_attachment_arguments_many, audio_file_metadata
from qwen_asr_app.model_runtime import ModelManager
from qwen_asr_app.ai_activity import track_ai_activity, task_ai_activity
from qwen_asr_app.request_security import websocket_origin_allowed, http_request_allowed
from qwen_asr_app.pdf_ai import find_mineru_binary
from qwen_asr_app.mineru_settings import mineru_settings, runtime_info, DEFAULT_MINERU_SETTINGS
from qwen_asr_app.pdf_jobs import PDFJobs
from qwen_asr_app.task_center import task_center_router
from qwen_asr_app.course_file_routes import CourseFileAPI
from qwen_asr_app.categories import change_category
from qwen_asr_app.shared_library import LibraryWatch, discover_courses
from qwen_asr_app.library_backup import BackupManager, backup_router, backup_sources
from qwen_asr_app.library_layout import shared
from qwen_asr_app.review import (
    bisect_audio_range,
    due_summary_windows,
    has_repetitive_tail,
    trim_repetitive_tail,
)
from qwen_asr_app.settings import (
    DEFAULT_AI_TITLE_PROMPT,
    DEFAULT_AI_SYSTEM_PROMPT,
    DEFAULT_NOTE_ABSTRACT_PROMPT,
    DEFAULT_NOTE_SUMMARY_PROMPT,
    DEFAULT_ASR_PARAMS,
    DEFAULT_LIVE_PARAMS,
    SAMPLE_RATE,
    normalize_asr_params,
    normalize_live_params,
)
from qwen_asr_app.storage import (
    RecordingRecord,
    app_settings,
    format_clock,
    local_now,
    recording_store,
    safe_component,
)
from qwen_asr_app.terminology import (
    asr_params_with_terminology,
    asr_terminology_context,
    normalize_terminology,
)
from qwen_asr_app.lazy import lazy_module
from qwen_asr_app import smartclass_service

np = lazy_module("numpy")

ROOT = Path(__file__).resolve().parent
if not os.getenv("QWEN_ASR_DATA_DIR"):
    load_dotenv(ROOT / ".env")
from qwen_asr_app.model_setup import model_path
MODEL_PATH = model_path()
model_manager = ModelManager(MODEL_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with recording_store.service_startup():
        # The course page used to run as a second uvicorn process on port 8766.
        # It now mounts below /course in this app, so enter its lifecycle here as
        # well.  This keeps credential recovery and the durable course queue alive
        # without opening another listener.
        async with smartclass_service.lifespan(smartclass_service.app):
            await library_watch.start()
            await knowledge_service.start()
            await knowledge_coordinator.start()
            intelligence_service = getattr(intelligence_router, 'intelligence_service', None)
            if intelligence_service and hasattr(intelligence_service, 'start'):
                await intelligence_service.start()
            from qwen_asr_app.remote_backlog import recovery_loop
            backlog=asyncio.create_task(recovery_loop(recording_store,model_manager,live_sessions,
                lambda ident: ident in record_operations or ident in finalization_tasks or ident in running_batch_ids or recording_discard.busy(ident),
                claim=backlog_record_claim,on_completed=backlog_completed,on_idle=schedule_memory_cleanup))
            correction_recovery=asyncio.create_task(resume_correction_verifications())
            try:
                yield
            finally:
                backlog.cancel()
                correction_recovery.cancel()
                await asyncio.gather(backlog,correction_recovery,return_exceptions=True)
                await recording_discard.close()
                intelligence_service = getattr(intelligence_router, 'intelligence_service', None)
                if intelligence_service and hasattr(intelligence_service, 'close'):
                    await intelligence_service.close()
                await knowledge_coordinator.close()
                await knowledge_service.close()
                from qwen_asr_app.knowledge.embeddings import close_embedder
                await asyncio.to_thread(close_embedder)
                await backup_manager.close()
                await library_watch.close()
                await course_stream_manager.close()
                tasks = [
                    *background_summary_tasks.values(),
                    *background_ai_tasks.values(),
                    *finalization_tasks.values(),
                    *pdf_ai_background_tasks.values(),
                    *course_file_api.tasks.values(),
                    *upload_jobs.tasks.values(),
                ]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await model_manager.close()
                await close_ai_clients()

app = FastAPI(title="Qwen3-ASR 本地语音工作台", lifespan=lifespan)
record_operations: set[str] = set()
summary_jobs: dict[str, dict[str, Any]] = {}
background_summary_tasks: dict[str, asyncio.Task[Any]] = {}
background_ai_tasks: dict[str, asyncio.Task[Any]] = {}
finalization_tasks: dict[str, asyncio.Task[Any]] = {}
live_sessions: dict[str, TranscriptionSession] = {}
classroom_chat_running: set[str] = set()
segment_retry_locks: dict[tuple[str, int], asyncio.Lock] = {}
pause_requested_tasks: set[str] = set()
cancel_requested_tasks: set[str] = set()
pdf_ai_lock = asyncio.Lock()
pdf_jobs = PDFJobs(pdf_ai_lock)
pdf_ai_jobs = pdf_jobs.jobs
pdf_ai_background_tasks = pdf_jobs.tasks
course_file_api = CourseFileAPI(app_settings, recording_store, ideashell_manager, pdf_ai_lock)
library_watch = LibraryWatch(app_settings, recording_store)
backup_manager = BackupManager()
app.include_router(course_file_api.router)
from qwen_asr_app.knowledge.service import KnowledgeService
from qwen_asr_app.knowledge.routes import knowledge_router
from qwen_asr_app.knowledge.config_routes import knowledge_config_router
from qwen_asr_app.classroom_intelligence.routes import classroom_intelligence_router
from qwen_asr_app.transcription_assist.service import configure_knowledge
knowledge_service = KnowledgeService(app_settings, recording_store, course_file_api.store)
configure_knowledge(knowledge_service)
from qwen_asr_app.knowledge.coordinator import KnowledgeCoordinator
knowledge_coordinator = KnowledgeCoordinator(knowledge_service)
app.include_router(knowledge_router(knowledge_service))
app.include_router(knowledge_config_router(knowledge_service))
from qwen_asr_app.transcription_assist.routes import transcription_assist_router
app.include_router(transcription_assist_router(recording_store,
    require_available=lambda ident:require_recording_available(ident)))
intelligence_router = classroom_intelligence_router(recording_store, knowledge_service, ai_preferences, ai_secret_store, live_sessions)
app.include_router(intelligence_router)
from qwen_asr_app.classroom_intelligence.service import configure_intelligence
configure_intelligence(intelligence_router.intelligence_service)
from qwen_asr_app.classroom_chat import classroom_chat_router
app.include_router(classroom_chat_router(recording_store, ai_preferences, ai_secret_store, live_sessions, classroom_chat_running, knowledge_service))


class EphemeralFileResponse(FileResponse):
    """Delete a generated download file after its response finishes or disconnects."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.to_thread(Path(self.path).unlink, missing_ok=True)


def recording_is_busy(session_id: str, *, owner: asyncio.Task[Any] | None = None) -> bool:
    discard = globals().get('recording_discard')
    if discard and discard.busy(session_id):
        return True
    if session_id in record_operations or session_id in running_batch_ids or session_id in classroom_chat_running:
        return True
    uploads = globals().get("upload_jobs")
    if uploads and any(job.get("recording_id") == session_id and ident in uploads.tasks
                       and uploads.tasks[ident] is not owner for ident, job in uploads.jobs.items()):
        return True
    tasks = [background_summary_tasks.get(session_id), finalization_tasks.get(session_id)]
    tasks.extend(task for key, task in background_ai_tasks.items() if key.endswith(":" + session_id))
    return any(task is not None and task is not owner and not task.done() for task in tasks)


def require_recording_available(session_id: str, *, owner: asyncio.Task[Any] | None = None) -> None:
    if recording_is_busy(session_id, owner=owner):
        raise HTTPException(
            status_code=409, detail="这条录音正在处理，请完成后再操作",
            headers={"Retry-After": "3"},
        )


@contextmanager
def backlog_record_claim(session_id):
    require_recording_available(session_id, owner=asyncio.current_task())
    record_operations.add(session_id)
    try:yield
    finally:record_operations.discard(session_id)


def backlog_completed(session_id):
    record=recording_store.find(session_id)
    if record is None or record.metadata.get('status')!='completed':return
    intent=record.metadata.get('summary_job') or {}
    if intent.get('status')=='queued' and intent.get('automatic') and not intent.get('cancel_requested'):
        start_summary_job(record,{'config':ai_preferences.task_config()},automatic=True)


SUMMARY_WORKFLOW_TIMEOUT_SECONDS = 7200
SUMMARY_SMALL_OPERATION_TIMEOUT_SECONDS = 1800


def schedule_memory_cleanup():
    from qwen_asr_app.runtime_memory import schedule_idle_reclaim
    def is_idle():
        tasks=[*background_summary_tasks.values(),*background_ai_tasks.values(),*finalization_tasks.values()]
        return (not record_operations and not running_batch_ids and not model_manager.active_jobs
            and not knowledge_service.job_manager._active_job_id and not knowledge_service.job_manager._queue.qsize()
            and not any(not task.done() for task in tasks)
            and not (recording_store.active and recording_store.active.metadata.get('status')=='active'))
    schedule_idle_reclaim(is_idle,lambda:recording_store._read_cache.clear())


def recording_operation(function):
    @wraps(function)
    async def guarded(session_id: str, *args, **kwargs):
        # A queued summary owns this record before its decorated runner starts.
        require_recording_available(session_id, owner=asyncio.current_task())
        record_operations.add(session_id)
        is_summary = function.__name__ in {"generate_library_summary", "generate_library_summary_abstract", "upgrade_library_note_pdf"}
        # Full illustrated notes include source reading, writing, LaTeX and
        # independent page review. Each request/compile already has its own
        # bounded timeout; the old single-response budget cut off live work.
        timeout_seconds = (SUMMARY_WORKFLOW_TIMEOUT_SECONDS
                           if function.__name__ in {"generate_library_summary", "upgrade_library_note_pdf"}
                           else SUMMARY_SMALL_OPERATION_TIMEOUT_SECONDS)
        if is_summary:
            summary_jobs[session_id] = {"task": asyncio.current_task(), "started": time.monotonic(),
                                        "timeout_seconds": timeout_seconds}
        try:
            if is_summary:
                return await asyncio.wait_for(function(session_id, *args, **kwargs), timeout=timeout_seconds)
            return await function(session_id, *args, **kwargs)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail=f"本次处理达到 {timeout_seconds // 60:g} 分钟总时限，已保存检查点；继续会复用已完成内容") from exc
        except asyncio.CancelledError as exc:
            if background_summary_tasks.get(session_id) is asyncio.current_task():
                # Let the background owner persist its paused/cancelled state.
                raise
            if is_summary:
                raise HTTPException(status_code=409, detail="已取消总结生成，已完成分块保留，下次可继续") from exc
            raise
        finally:
            if is_summary:
                summary_jobs.pop(session_id, None)
            record_operations.discard(session_id)
            schedule_memory_cleanup()
    return guarded


def _remember_ai_keys(config: Any) -> None:
    if os.getenv("QWEN_ASR_TESTING") == "1":
        # Test fixtures exercise the legacy callback contract without a real
        # durable settings file; production never takes this branch.
        if isinstance(config, dict):
            from qwen_asr_app.ai import provider_for_mode
            ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
            values = {mode: str((ai.get(mode + "_provider") or {}).get("api_key") or "") for mode in ("sentence", "review", "summary", "correction", "global")}
            if any(values.values()):
                ai_secret_store.update(values, {mode: provider_for_mode(ai, mode).get("base_url") for mode in values})
        return
    # Request payloads may have come from a stale browser cache. Credentials
    # are written only by PUT /api/ai/settings (the explicit save operation),
    # never while starting, retrying, or streaming an AI task.
    return


def _saved_ai_task_config(record: RecordingRecord, kind: str) -> dict[str, Any]:
    """Build task configuration only from the server-side saved AI settings.

    Task actions arrive from a browser, but the browser is not an authority for
    providers or credentials. In particular, an empty or stale localStorage
    value must never silently turn a saved SiliconFlow/Qwen setup into the
    OpenAI default.
    """
    required_mode = {"refinement": "correction", "live_summary": "review", "summary": "summary"}.get(kind, "correction")
    course = record.metadata.get("course_settings") or {}
    config = ai_preferences.task_config({}, course=course, required_modes=(required_mode,))
    if kind == "refinement":
        config.update(ai_correction=True, translation="off")
    elif kind == "live_summary":
        config.update(summary_enabled=True)
    elif kind == "summary":
        config["_note_summary_system_prompt"] = config.get("_note_summary_system_prompt") or DEFAULT_NOTE_SUMMARY_PROMPT
        config["_note_abstract_system_prompt"] = config.get("_note_abstract_system_prompt") or DEFAULT_NOTE_ABSTRACT_PROMPT
    return config


@app.get("/api/ai/secrets")
async def ai_secret_status() -> dict[str, Any]:
    return {"saved": ai_secret_store.status(), "storage": "local_user_only"}


@app.get('/api/ai/settings')
async def get_ai_settings():
    try:
        return {'settings': ai_preferences.read(), 'saved': ai_secret_store.status()}
    except (OSError, ValueError) as exc:
        raise HTTPException(500, '本机 AI 配置读取失败，未用默认值覆盖') from exc


@app.get('/api/mineru/settings')
async def get_mineru_settings():
    try:
        return {"settings": mineru_settings.read(), "defaults": DEFAULT_MINERU_SETTINGS,
                "runtime": runtime_info(find_mineru_binary())}
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "本机 MinerU 配置读取失败，未用默认值覆盖") from exc


@app.get('/api/inference/settings')
async def get_inference_settings():
    from qwen_asr_app.inference_providers import inference_settings, DEFAULTS
    try:
        return {"settings": inference_settings.read(), "defaults": DEFAULTS}
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "推理配置读取失败") from exc


@app.put('/api/inference/settings')
async def save_inference_settings(payload: dict[str, Any] = Body(...)):
    from qwen_asr_app.inference_providers import inference_settings
    try:
        cfg=inference_settings.save(payload.get('settings'))
        for session in list(live_sessions.values()):
            session.update_config({'inference_settings':cfg})
        if cfg['asr_provider']=='remote_only':await model_manager.release_if_idle()
        if cfg['knowledge_provider']!='local':
            from qwen_asr_app.knowledge.embeddings import close_embedder
            await asyncio.to_thread(close_embedder)
        return {"settings":cfg,'message':'设置已保存；新音频从下一段生效，已提交任务沿用其快照'}
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post('/api/inference/test')
async def test_inference_connection(payload: dict[str, Any] = Body(...)):
    import httpx
    from qwen_asr_app.inference_providers import normalize, remote_client
    try:
        cfg = normalize(payload.get('settings'))
        health = await asyncio.to_thread(lambda: remote_client(cfg).health())
        from qwen_asr_app.remote_classroom import capabilities
        try:
            caps=await asyncio.to_thread(capabilities,cfg)
        except RuntimeError:
            caps={'version':'旧节点','vectors':False,'asr_session':False}
        return {'health': health,'capabilities':caps}
    except Exception as exc:
        if isinstance(exc, httpx.ConnectTimeout):
            message = '远程连接超时，请检查 Windows Tailscale 节点和内网服务入口'
        elif isinstance(exc, httpx.TransportError):
            message = '远程网络连接中断，请稍后重试；新入口应使用 Windows 原生 Tailscale 地址'
        elif str(exc) == 'REMOTE_HTTP_401':
            message = '远程鉴权失败，请核对本机受保护的访问令牌'
        elif str(exc) == 'REMOTE_HTTP_403':
            message = '远程服务拒绝此连接来源，请核对 Tailscale Serve 和后端访问规则'
        else:
            message = '无法连接远程网关：' + type(exc).__name__
        raise HTTPException(400, message) from exc


@app.put('/api/mineru/settings')
async def save_mineru_settings(payload: dict[str, Any] = Body(...)):
    try:
        return {"settings": mineru_settings.save(payload.get("settings")),
                "runtime": runtime_info(find_mineru_binary())}
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put('/api/ai/settings')
async def save_ai_settings(payload: dict[str, Any] = Body(...)):
    try:
        settings = ai_preferences.save(payload.get('settings'), ai_secret_store)
        return {'settings': settings, 'saved': ai_secret_store.status()}
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete('/api/ai/settings')
async def clear_ai_settings():
    ai_preferences.clear()
    ai_secret_store.clear()
    return {'cleared': True}


@app.put("/api/ai/secrets")
async def save_ai_secrets(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"saved": ai_secret_store.update(payload.get("keys"), payload.get("base_urls"))}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/ai/secrets")
async def clear_ai_secrets() -> dict[str, bool]:
    ai_secret_store.clear()
    return {"cleared": True}


@app.post("/api/ai/test")
async def test_provider(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="AI 配置格式无效")
    mode = str(payload.get("mode") or "sentence")
    required_mode = mode if mode in {"sentence", "review", "summary", "correction", "global"} else "sentence"
    try:
        # The test button is intentionally ephemeral: it may validate a cheap
        # AGY model before the user saves it. Task actions still use only the
        # durable server settings through ``_saved_ai_task_config``.
        candidate_ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
        candidate = candidate_ai if required_mode == "sentence" else candidate_ai.get(required_mode + "_provider")
        if not isinstance(candidate, dict):
            candidate = {}
        transport = str(candidate.get("transport") or "api").strip().lower()
        has_candidate = bool(
            (transport == "agy_cli" and (candidate.get("cli_model") or candidate.get("model")))
            or (transport == "api" and candidate.get("base_url") and candidate.get("model"))
        )
        if has_candidate:
            config = copy.deepcopy(config)
            config["ai"] = copy.deepcopy(candidate_ai)
            # Validate the ephemeral provider through the same durable schema
            # without writing it. This also rejects malformed extra_params.
            params = candidate.get("extra_params", {})
            if isinstance(params, str):
                params = json.loads(params)
            if not isinstance(params, dict):
                raise ValueError(f"{required_mode} AI 参数必须是 JSON 对象")
            if transport == "api":
                from qwen_asr_app.ai_secrets import endpoint_identity
                endpoint_identity(candidate.get("base_url"))
        else:
            config = ai_preferences.task_config(config, required_modes=(required_mode,))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        selected_provider = provider_for_mode(config.get("ai", {}), required_mode)
        test_timeout = 45
        if provider_transport(selected_provider) == "agy_cli":
            from qwen_asr_app.agy_cli import timeout_seconds
            test_timeout = min(180, timeout_seconds(selected_provider))
        return await asyncio.wait_for(test_ai_connection(config, mode), timeout=test_timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"{test_timeout} 秒内未完成测试，请检查网络、服务地址或模型响应速度") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/recordings/{session_id}/summary-status")
async def summary_status(session_id: str) -> dict[str, Any]:
    job = summary_jobs.get(session_id)
    record = recording_store.find(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    draft = record.metadata.get("ai_note_summary_draft") or {}
    progress = _summary_phase_progress(record, draft)
    saved_job = record.metadata.get("summary_job") or {}
    return {"running": bool(job) or session_id in background_summary_tasks,
            "status": saved_job.get("status", ""), "error": saved_job.get("error", ""),
            "completed_parts": progress['completed_parts'],
            "total_parts": progress['total_parts'], "progress_unit": progress.get('unit', '批'),
            "phase": (job or {}).get("phase") or progress['phase'],
            "elapsed_seconds": round(time.monotonic() - job["started"]) if job else 0,
            "has_summary": bool(record.metadata.get("ai_note_summary", {}).get("summary")),
            "review_status": (record.metadata.get('ai_note_summary') or {}).get('review_status','unreviewed')}


@app.post('/api/recordings/{session_id}/review-pdf')
async def start_review_pdf(session_id: str):
    record=recording_store.find(session_id)
    if record is None:raise HTTPException(404,'没有找到这次录音')
    if record.metadata.get('status')!='completed':raise HTTPException(409,'请先保存录音')
    start_summary_job(record,{'_pdf_only':True})
    return {'queued':True,'message':'正在导出并逐页独立审查；已有正文保留'}


@app.get('/api/recordings/{session_id}/review-materials')
async def review_material_choices(session_id: str):
    from qwen_asr_app.course_files import CourseFiles
    record=recording_store.find(session_id)
    if record is None:raise HTTPException(404,'没有找到这次录音')
    return {'selected':record.metadata.get('review_material_ids'),'materials':[
        {'id':i['id'],'name':i['name'],'sha256':i['sha256']} for i in CourseFiles(app_settings).list(record.metadata['category'])]}


@app.get('/api/recordings/{session_id}/note-proposal')
async def get_note_proposal(session_id: str):
    from qwen_asr_app.review_materials import safe_path
    record=recording_store.find(session_id)
    if record is None:raise HTTPException(404,'没有找到这次录音')
    ident=(record.metadata.get('ai_note_proposal') or {}).get('task_id','')
    if not re.fullmatch(r'[a-f0-9]{24}',ident):raise HTTPException(404,'没有待采用的候选版本')
    path=safe_path(record.directory,f'.review-notes/{ident}/proposed-note.json')
    if not path.is_file():raise HTTPException(404,'候选版本不存在')
    note=json.loads(path.read_text())
    return {'note':note,'task_id':ident,'current_summary':(record.metadata.get('ai_note_summary') or {}).get('summary','')}


@app.post('/api/recordings/{session_id}/note-proposal/adopt')
@recording_operation
async def adopt_note_proposal(session_id: str,payload:dict=Body(...)):
    from qwen_asr_app.note_pdf import reviewed_pdf
    from types import SimpleNamespace
    record=recording_store.find(session_id)
    proposal=await get_note_proposal(session_id)
    if payload.get('task_id')!=proposal['task_id'] or payload.get('expected_summary')!=proposal['current_summary']:
        raise HTTPException(409,'正文或候选版本已改变，请重新预览后采用')
    note=proposal['note']
    try:await asyncio.to_thread(reviewed_pdf,SimpleNamespace(directory=record.directory,metadata={'ai_note_summary':note}))
    except (ValueError,RuntimeError) as exc:raise HTTPException(409,'候选版本尚未通过当前视觉审查：'+str(exc)) from exc
    record.metadata['ai_note_summary']=note
    record.metadata.pop('ai_note_proposal',None)
    # Adopting a note changes the note only. save_all also regenerates text
    # transcripts, which can contain the prior summary or user edits.
    record.save_metadata()
    record.save_summary_markdown()
    return {'summary':note,'session':record.public()}


@app.put('/api/recordings/{session_id}/review-materials')
@recording_operation
async def save_review_material_choices(session_id: str,payload:dict=Body(...)):
    from qwen_asr_app.course_files import CourseFiles
    record=recording_store.find(session_id)
    if record is None:raise HTTPException(404,'没有找到这次录音')
    ids=payload.get('material_ids')
    valid={i['id'] for i in CourseFiles(app_settings).list(record.metadata['category'])}
    if ids is not None and (not isinstance(ids,list) or any(not isinstance(i,str) or i not in valid for i in ids)):
        raise HTTPException(400,'所选课件不属于本课程或已移除')
    record.metadata['review_material_ids']=ids
    record.save_metadata()
    return {'saved':True,'message':'已保存本节课件范围，继续生成时会复用输入不变的阶段'}


@recording_operation
async def upgrade_library_note_pdf(session_id: str, payload):
    from qwen_asr_app.review_notes import upgrade_pdf
    record=recording_store.find(session_id)
    if record is None:raise HTTPException(404,'没有找到这次录音')
    result=await upgrade_pdf(record,payload['config'])
    return {'summary':record.metadata.get('ai_note_summary'),'review':result}


@app.get('/api/recordings/{session_id}/note-assets/{task_id}/{asset}')
async def note_asset(session_id: str, task_id: str, asset: str):
    from qwen_asr_app.review_materials import safe_path
    record=recording_store.find(session_id)
    if record is None or not re.fullmatch(r'(?:legacy-)?[a-f0-9]{24}',task_id) or not re.fullmatch(r'fig-[a-f0-9]{16}\.png',asset):
        raise HTTPException(404,'笔记图片不存在')
    path=safe_path(record.directory,f'.review-notes/{task_id}/assets/{asset}')
    if not path.is_file():raise HTTPException(404,'笔记图片不存在')
    return FileResponse(path,media_type='image/png',headers={'Cache-Control':'private, max-age=31536000, immutable'})


@app.post("/api/recordings/{session_id}/cancel-summary")
async def cancel_summary(session_id: str) -> dict[str, Any]:
    record = recording_store.find(session_id)
    background = background_summary_tasks.get(session_id)
    if background:
        cancel_requested_tasks.add(f"summary:{session_id}")
        background.cancel()
        return {"cancelled": True}
    job = summary_jobs.get(session_id)
    if job:
        cancel_requested_tasks.add(f"summary:{session_id}")
        job["task"].cancel()
        return {"cancelled": True}
    # The recording tail may still be saving while the automatic summary is
    # queued.  Cancel only the AI intent, never the audio finalization.
    saved = record.metadata.get("summary_job") if record else None
    if isinstance(saved, dict) and saved.get("status") in {"queued", "waiting_retry", "paused", "failed", "interrupted", "completed_with_errors"}:
        saved.update(
            status="cancelled",
            cancel_requested=True,
            error="任务已取消；已完成内容仍然保留",
            updated_at=local_now(),
        )
        record.save_metadata()
        return {"cancelled": True}
    return {"cancelled": False}


AI_TASK_LABELS = {
    "summary": "课后复习笔记",
    "refinement": "转写校对",
    "live_summary": "课堂实时提纲",
}


def _saved_ai_job(record: RecordingRecord, kind: str) -> dict[str, Any]:
    jobs = record.metadata.setdefault("ai_jobs", {})
    if not isinstance(jobs, dict):
        jobs = {}
        record.metadata["ai_jobs"] = jobs
    job = jobs.setdefault(kind, {})
    if not isinstance(job, dict):
        job = {}
        jobs[kind] = job
    return job


def _update_ai_job(record: RecordingRecord, kind: str, **values: Any) -> None:
    job = _saved_ai_job(record, kind)
    if values.get('status') in {'queued', 'running', 'waiting_retry'}:
        job.pop('task_hidden', None)
    job.update(values, updated_at=local_now())
    record.save_metadata()


def _task_error_kind(error: str) -> str:
    value = str(error or "").lower()
    # These are the redacted material-channel messages, not raw CLI output.
    # Keep their transport category when a public HTTP boundary wraps them.
    if "agy 资料会话" in value:
        if "连接中断" in value:
            return "network_transport"
        if "连接超时" in value:
            return "network_connect_timeout"
        if "响应超时" in value:
            return "network_read_timeout"
    if "agy cli" in value:
        from qwen_asr_app.agy_cli import _error_kind as agy_error_kind
        kind, _ = agy_error_kind(value)
        if kind != "agy_cli_error":
            return kind
    if any(marker in value for marker in ("nodename nor servname", "name resolution", "dns ")):
        return "network_dns"
    if "达到长度上限" in value or "被截断" in value or "finish_reason" in value:
        return "output_truncated"
    if any(marker in value for marker in ("insufficient_quota", "quota exceeded", "余额不足", "额度耗尽", "配额耗尽", "欠费")):
        return "provider_quota"
    if "429" in value or "限流" in value or "rate limit" in value:
        return "rate_limit"
    if "status=502" in value or "status=503" in value or "status=504" in value or "服务繁忙" in value:
        return "provider_busy"
    if any(marker in value for marker in ("context length", "maximum context", "input too long", "request too large", "too many tokens", "片段长度", "上下文长度")):
        return "input_too_long"
    if "expecting ',' delimiter" in value or "损坏的 json" in value or "无法解析的 json" in value:
        return "invalid_json"
    if "段落顺序" in value or "不相邻段落" in value or "段落映射" in value:
        return "invalid_segment_mapping"
    return "upstream"


def _review_batch_activity(folder, task):
    path=folder/str(task.get('review_report') or '.missing')
    if not path.resolve().is_relative_to(folder.resolve()) or not path.is_file():return []
    report=json.loads(path.read_text())
    if report.get('pdf_sha256')!=task.get('pdf_sha256'):return []
    return [{key:value[key] for key in ('pages','attempt','started_at','timeout_seconds','state') if key in value}
            for value in report.get('active_batches',{}).values() if isinstance(value,dict)]


def _summary_phase_progress(record, draft):
    """Show the actual current writing phase, never an earlier plan's 6/6."""
    value = {'phase': str(draft.get('phase') or ''),
             'completed_parts': len(draft.get('parts') or []),
             'total_parts': draft.get('total_parts') or 0}
    task_id = str(draft.get('review_task') or '')
    if task_id and not value['phase'].startswith(('识别本节新内容','LaTeX 排版','独立审查')):
        value.update(completed_parts=0,total_parts=0)
    if not re.fullmatch(r'[a-f0-9]{24}', task_id):
        return value
    try:
        folder = record.directory / '.review-notes' / task_id
        if value['phase'].startswith('独立审查'):
            task = json.loads((folder / 'task.json').read_text())
            total, completed = task.get('review_total_pages'), task.get('reviewed_pages')
            if (type(total) is int and type(completed) is int and 1 <= total <= 10000
                    and 0 <= completed <= total and task.get('id') == task_id
                    and re.fullmatch(r'[a-f0-9]{64}', str(task.get('pdf_sha256') or ''))):
                return {'phase': f'独立审查 · 已查看 {completed}/{total} 页',
                        'completed_parts': completed, 'total_parts': total, 'unit': '页',
                        'review_round':int(task.get('revision') or 0)+1,
                        'review_batches':_review_batch_activity(folder,task)}
            return value
        if not value['phase'].startswith('LaTeX 排版'):
            return value
        path = folder / 'latex' / 'batch-progress.json'
        if not path.resolve().is_relative_to(record.directory.resolve()) or not path.is_file() or path.stat().st_size > 32000:
            return value
        progress = json.loads(path.read_text())
        total, completed = progress['total_batches'], progress['completed_batches']
        if (type(total) is not int or type(completed) is not int or not 1 <= total <= 10000
                or not 0 <= completed <= total
                or progress.get('binding', {}).get('markdown_sha256') != hashlib.sha256((folder / 'note.md').read_bytes()).hexdigest()):
            return value
        current = min(total, completed + 1)
        return {'phase': f'LaTeX 排版 · 已保存 {completed}/{total} 批' if completed == total else f'LaTeX 排版 · 第 {current}/{total} 批',
                'completed_parts': completed, 'total_parts': total}
    except (OSError, ValueError, KeyError, TypeError):
        return value


AUTO_RETRY_KINDS = {
    "rate_limit", "provider_busy", "provider_unavailable", "network_dns", "network_connect",
    "network_connect_timeout", "network_read_timeout", "network_transport",
}
MAX_AUTOMATIC_RETRIES = 6


def _error_kind(exc: BaseException) -> str:
    # A route translates provider errors to HTTP responses for clients. The
    # server-owned retry still has the original typed cause and must retain it.
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        kind = getattr(current, "kind", "")
        if kind:
            return str(kind)
        current = current.__cause__
    if isinstance(exc, HTTPException):
        return _task_error_kind(str(exc.detail))
    return str(getattr(exc, "kind", "") or _task_error_kind(str(exc)))


def _automatic_retry_delay(attempt: int) -> int:
    return min(300, 15 * (2 ** max(0, attempt - 1)))


def _deduplicated_live_summaries(record: RecordingRecord) -> tuple[list[dict[str, Any]], dict[int, int]]:
    by_end: dict[int, dict[str, Any]] = {}
    attempts: dict[int, int] = {}
    for raw in record.metadata.get("ai_summaries") or []:
        if not isinstance(raw, dict):
            continue
        end = int(round(float(raw.get("end", 0))))
        attempts[end] = attempts.get(end, 0) + 1
        previous = by_end.get(end)
        # A successful cumulative checkpoint is more useful than later failed
        # duplicates produced by older versions' retry storm.
        if previous is None or (previous.get("ai_error") and not raw.get("ai_error")):
            by_end[end] = dict(raw)
        elif previous.get("ai_error") and raw.get("ai_error"):
            by_end[end] = dict(raw)
    return [by_end[key] for key in sorted(by_end)], attempts


def _summary_abstract_pending(record: RecordingRecord) -> bool:
    """Only resume an abstract when this generation saved its detailed note."""
    note = record.metadata.get("ai_note_summary") or {}
    job = record.metadata.get("summary_job") or {}
    draft = record.metadata.get("ai_note_summary_draft") or {}
    if not note.get("summary") or str(note.get("abstract") or "").strip():
        return False
    if job.get("resume_stage") == "abstract":
        return True
    if draft.get("phase") != "录音库短摘要":
        return False
    try:
        return datetime.fromisoformat(note["updated_at"]) >= datetime.fromisoformat(job["started_at"])
    except (KeyError, TypeError, ValueError):
        return False


def compact_ai_tasks(include_history: bool = False) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    try:
        saved_providers = (ai_preferences.read().get("function_providers") or {})
    except (OSError, ValueError):
        saved_providers = {}

    def ai_provider_snapshot(kind: str) -> dict[str, Any]:
        mode = {"summary": "summary", "refinement": "correction", "live_summary": "review"}.get(kind, "sentence")
        provider = saved_providers.get(mode) if isinstance(saved_providers, dict) else {}
        provider = provider if isinstance(provider, dict) else {}
        transport = str(provider.get("transport") or "api")
        return {
            "configured_ai_transport": transport,
            "configured_ai_model": str(provider.get("cli_model") or provider.get("model") or ""),
            "configured_ai_effort": str(provider.get("cli_effort") or ""),
        }

    for record in getattr(recording_store,'iter_records',recording_store.list_records)():
        base = {
            "recording_id": record.id,
            "recording_title": str(record.metadata.get("title") or "未命名录音"),
            "category": str(record.metadata.get("category") or "未分类"),
        }
        pending=record.metadata.get('pending_segments') or {}
        if pending and any((p.get('config_snapshot') or {}).get('inference_settings',{}).get('asr_provider')=='remote_only' for p in pending.values()):
            backlog=record.metadata.get('remote_backlog') or {}
            status=backlog.get('status','pending')
            tasks.append({**base,'id':f'remote_asr:{record.id}','type':'remote_asr','status':status,
                'phase':backlog.get('message','录音已保存，等待 Windows 补转'),
                'total':len(pending),'completed':0,'configured_provider':'remote_only','actual_provider':'remote',
                'actions':['continue','cancel'] if status in {'paused','cancelled'} else ['pause','cancel'],
                'updated_at':record.metadata.get('updated_at')})
        summary = record.metadata.get("summary_job") or {}
        if isinstance(summary, dict) and summary.get("status") in {"queued", "running", "waiting_retry", "paused", "failed", "interrupted", "completed_with_errors"}:
            draft = record.metadata.get("ai_note_summary_draft") or {}
            summary_progress = _summary_phase_progress(record, draft)
            status = str(summary.get("status"))
            abstract_pending = _summary_abstract_pending(record)
            if abstract_pending and status in {"failed", "interrupted"}:
                status = "completed_with_errors"
            tasks.append({
                **base,
                **ai_provider_snapshot("summary"),
                "id": f"summary:{record.id}",
                "type": "summary",
                "label": AI_TASK_LABELS["summary"],
                "status": status,
                "error": str(summary.get("error") or ""),
                "error_kind": _task_error_kind(str(summary.get("error") or "")),
                "completed": summary_progress['completed_parts'],
                "total": int(summary_progress['total_parts'] or 0),
                "progress_unit": summary_progress.get('unit', '批'),
                "review_round":summary_progress.get('review_round'),
                "review_batches":summary_progress.get('review_batches',[]),
                "phase": ("详细笔记已保存 · 短摘要待补齐" if abstract_pending and status == "completed_with_errors"
                          else "录音库短摘要" if abstract_pending else summary_progress['phase']),
                "ai_activity": task_ai_activity(f"summary:{record.id}"),
                "resume_stage": "abstract" if abstract_pending else "",
                "retry_in_seconds": int(summary.get("retry_in_seconds") or 0),
                "auto_retry_count": int(summary.get("auto_retry_count") or 0),
                "actions": (["pause", "cancel"] if status in {"queued", "running", "waiting_retry"} else ["continue", "retry", "cancel"]),
                "updated_at": str(summary.get("updated_at") or summary.get("started_at") or ""),
            })

        refinement_job = _saved_ai_job(record, "refinement")
        missing_blocks = [block for block in transcript_blocks(record) if not block["refined"]]
        review_blocks = sum(bool(block.get("correction_review")) for block in missing_blocks)
        unprocessed_blocks = len(missing_blocks) - review_blocks
        from qwen_asr_app.refinement import saved_progress
        refinement_progress=saved_progress(record)
        refinement_running = f"refinement:{record.id}" in background_ai_tasks
        live_session = live_sessions.get(record.id)
        if live_session and (
            live_session.periodic_tasks.get("review")
            and not live_session.periodic_tasks["review"].done()
        ):
            refinement_running = True
        refinement_ready = record.metadata.get("status") not in {"active", "processing"}
        if refinement_running or refinement_job.get("status") in {"queued", "running", "waiting_retry", "paused", "failed", "interrupted", "cancelled"} or (refinement_ready and missing_blocks):
            failed_errors = [
                str(item.get("ai_error")) for item in [*record.segments, *(record.metadata.get("ai_reviews") or [])]
                if isinstance(item, dict) and item.get("ai_error")
            ]
            saved_status = str(refinement_job.get("status") or "")
            status = saved_status if refinement_running and saved_status == "waiting_retry" else (
                "running" if refinement_running else saved_status or ("failed" if failed_errors else "pending")
            )
            if status == "completed" and missing_blocks:
                status = "pending"
            if review_blocks and not unprocessed_blocks and not refinement_running and status in {'completed','pending'}:
                status='pending'
            if status != "completed":
                historical_error = failed_errors[-1] if failed_errors else ""
                retrying = refinement_running and saved_status in {"queued", "running"}
                task_error = str(refinement_job.get("error") or ("" if retrying else historical_error))
                if review_blocks and not unprocessed_blocks and not refinement_running:task_error=''
                review_only = bool(review_blocks and not unprocessed_blocks and not task_error
                                   and not refinement_running and status == "pending")
                tasks.append({
                    **base,
                    **ai_provider_snapshot("refinement"),
                    "id": f"refinement:{record.id}",
                    "type": "refinement",
                    "label": "AI 自动复核" if review_only else AI_TASK_LABELS["refinement"],
                    "status": status,
                    "error": task_error,
                    "error_kind": str(refinement_job.get("error_kind") or _task_error_kind(task_error)) if task_error else "",
                    "previous_error": historical_error if retrying else "",
                    "ai_activity": task_ai_activity(f"refinement:{record.id}"),
                    "completed": refinement_progress['saved_segments'],
                    "total": refinement_progress['total_segments'],
                    "progress_unit": "段",
                    "saved_segments": refinement_progress['saved_segments'],
                    "remaining_segments": refinement_progress['remaining_segments'],
                    "total_batches": int(refinement_job.get('total_batches') or 0),
                    "completed_batches": int(refinement_job.get('completed_batches') or 0),
                    "missing_blocks": len(missing_blocks),
                    "review_required_blocks": review_blocks,
                    "unprocessed_blocks": unprocessed_blocks,
                    "attention_kind": "auto_review" if review_only else "error" if task_error else "pending",
                    "phase": f"AI 自动复核 · {review_blocks} 段改动待检查" if review_only else str(refinement_job.get('phase') or ''),
                    "retry_in_seconds": int(refinement_job.get("retry_in_seconds") or 0),
                    "auto_retry_count": int(refinement_job.get("auto_retry_count") or 0),
                    "actions": ["pause", "cancel"] if status in {"queued", "running", "waiting_retry"} else ["continue", "retry", "cancel"],
                    "updated_at": str(refinement_job.get("updated_at") or record.metadata.get("updated_at") or ""),
                })

        summaries, attempts = _deduplicated_live_summaries(record)
        failed = next((item for item in summaries if item.get("ai_error")), None)
        live_job = _saved_ai_job(record, "live_summary")
        live_running = f"live_summary:{record.id}" in background_ai_tasks
        live_session = live_sessions.get(record.id)
        if live_session and (live_session.periodic_tasks.get("summary") and not live_session.periodic_tasks["summary"].done()):
            live_running = True
        live_cancelled = live_job.get("status") == "cancelled" and not live_running
        if not live_cancelled and (failed or live_running or live_job.get("status") in {"queued", "running", "waiting_retry", "paused", "failed", "interrupted"}):
            saved_status = str(live_job.get("status") or "")
            status = saved_status if live_running and saved_status == "waiting_retry" else (
                "running" if live_running else saved_status or "failed"
            )
            if status != "completed":
                historical_error = str((failed or {}).get("ai_error") or "")
                retrying = live_running and saved_status in {"queued", "running"}
                error = str(live_job.get("error") or ("" if retrying else historical_error))
                failed_end = int(round(float((failed or {}).get("end", 0))))
                tasks.append({
                    **base,
                    **ai_provider_snapshot("live_summary"),
                    "id": f"live_summary:{record.id}",
                    "type": "live_summary",
                    "label": AI_TASK_LABELS["live_summary"],
                    "status": status,
                    "error": error,
                    "previous_error": historical_error if retrying else "",
                    "ai_activity": task_ai_activity(f"live_summary:{record.id}"),
                    "error_kind": str(live_job.get("error_kind") or (failed or {}).get("error_kind") or _task_error_kind(error)) if error else "",
                    "completed": int(live_job.get("completed") or len([item for item in summaries if item.get("summary") and not item.get("ai_error")])),
                    "total": int(live_job.get("total") or len(summaries)),
                    "range_start": float((failed or {}).get("start", 0)),
                    "range_end": float((failed or {}).get("end", 0)),
                    "attempts": attempts.get(failed_end, 0),
                    "retry_in_seconds": int(live_job.get("retry_in_seconds") or 0),
                    "auto_retry_count": int(live_job.get("auto_retry_count") or 0),
                    "actions": ["pause", "cancel"] if status in {"queued", "running", "waiting_retry"} else ["continue", "retry", "cancel"],
                    "updated_at": str(live_job.get("updated_at") or record.metadata.get("updated_at") or ""),
                })
        if include_history:
            existing_ids = {item["id"] for item in tasks}
            for kind in AI_TASK_LABELS:
                saved = record.metadata.get("summary_job" if kind == "summary" else "ai_jobs", {})
                if kind != "summary":
                    saved = saved.get(kind, {})
                status = saved.get("status")
                ident = f"{kind}:{record.id}"
                if ident not in existing_ids and status in {"completed", "cancelled"} and not saved.get('task_hidden'):
                    tasks.append({**base, "id": ident, "type": kind, "label": AI_TASK_LABELS[kind],
                                  **ai_provider_snapshot(kind),
                                  "status": status, "completed": saved.get("completed", 0),
                                  "total": saved.get("total", 0), "progress": 100 if status == "completed" else 0,
                                  "updated_at": saved.get("updated_at", ""), "error": "",
                                  "actions": ["continue", "retry"] if status == "cancelled" else []})
    order = {"running": 0, "queued": 1, "waiting_retry": 2, "failed": 3, "interrupted": 4, "paused": 5, "pending": 6}
    return sorted(tasks, key=lambda item: (order.get(item["status"], 9), item.get("updated_at", "")), reverse=False)


@app.get("/api/ai-tasks")
async def ai_tasks() -> dict[str, Any]:
    tasks = compact_ai_tasks()
    return {
        "tasks": tasks,
        "active_count": len(tasks),
        "attention_count": sum(
            item["status"] in {"failed", "interrupted", "paused", "pending"} for item in tasks
        ),
    }


def _start_aux_ai_job(record: RecordingRecord, kind: str, runner: Any) -> bool:
    task_id = f"{kind}:{record.id}"
    current = background_ai_tasks.get(task_id)
    if current is not None and not current.done():
        return False
    require_recording_available(record.id)
    cancel_requested_tasks.discard(task_id)
    _update_ai_job(
        record, kind, status="queued", error="", error_kind="", started_at=local_now(),
        **({} if kind == 'refinement' else {'completed':0}),
        auto_retry_count=0, retry_in_seconds=0, pause_requested=False,
    )

    async def execute() -> None:
        automatic_attempt = 0
        try:
            while True:
                try:
                    _update_ai_job(record, kind, status="running", error="", error_kind="", retry_in_seconds=0)
                    await runner()
                    _update_ai_job(
                        record, kind, status="completed", error="", error_kind="",
                        auto_retry_count=automatic_attempt, retry_in_seconds=0,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_kind = _error_kind(exc)
                    automatic_attempt += 1
                    if error_kind in AUTO_RETRY_KINDS and automatic_attempt <= MAX_AUTOMATIC_RETRIES:
                        delay = _automatic_retry_delay(automatic_attempt)
                        _update_ai_job(
                            record, kind, status="waiting_retry", error=str(exc),
                            error_kind=error_kind, auto_retry_count=automatic_attempt,
                            retry_in_seconds=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    _update_ai_job(
                        record, kind, status="failed", error=str(exc), error_kind=error_kind,
                        auto_retry_count=automatic_attempt, retry_in_seconds=0,
                    )
                    return
        except asyncio.CancelledError:
            job = _saved_ai_job(record, kind)
            paused = task_id in pause_requested_tasks or bool(job.get("pause_requested"))
            pause_requested_tasks.discard(task_id)
            _update_ai_job(
                record, kind,
                status="paused" if paused else "cancelled" if task_id in cancel_requested_tasks else "interrupted",
                error="任务已暂停；已完成内容保留，可继续" if paused else "任务已取消；已完成内容保留，可继续",
                error_kind="paused" if paused else "cancelled",
                retry_in_seconds=0,
                pause_requested=False,
            )
            raise
        finally:
            background_ai_tasks.pop(task_id, None)
            schedule_memory_cleanup()

    async def run() -> None:
        with track_ai_activity(task_id):
            await execute()

    task = asyncio.create_task(run())
    background_ai_tasks[task_id] = task

    def cleanup(done: asyncio.Task[Any]) -> None:
        # A task cancelled before its first turn never enters run()'s finally.
        if background_ai_tasks.get(task_id) is not done:
            return
        background_ai_tasks.pop(task_id, None)
        if done.cancelled():
            current_record = recording_store.find(record.id)
            paused = task_id in pause_requested_tasks
            pause_requested_tasks.discard(task_id)
            if current_record is not None:
                _update_ai_job(
                    current_record, kind, status="paused" if paused else "cancelled" if task_id in cancel_requested_tasks else "interrupted",
                    error="任务已暂停，可继续" if paused else "任务已取消，可继续",
                    error_kind="paused" if paused else "cancelled",
                    retry_in_seconds=0, pause_requested=False,
                )

    task.add_done_callback(cleanup)
    return True


def _unrefined_segments(record: RecordingRecord) -> list[dict[str, Any]]:
    from qwen_asr_app.refinement import pending_ids
    ids=pending_ids(record)
    return [segment for segment in record.segments if int(segment.get("id", 0)) in ids]



_EMPTY_SPEECH_RE = re.compile(r"^[\s，。！？,.!?、]*(?:啊|哎|诶|嗯|呃|额|唔|那个|就是说)[\s，。！？,.!?、]*$")


async def _run_refinement_job_unlocked(record: RecordingRecord, config: dict[str, Any]) -> None:
    from qwen_asr_app.refinement import restore_checkpoints, commit_blocks, saved_progress, context_for
    if not config.get('_force_refinement'):restore_checkpoints(record)
    targets = _unrefined_segments(record)
    suppressed = [
        segment for segment in targets
        if _EMPTY_SPEECH_RE.fullmatch(str(segment.get("raw") or ""))
    ]
    if suppressed:
        for segment in suppressed:
            segment["ai_suppressed"] = True
            segment["ai_pending"] = False
            segment.pop("ai_error", None)
        record.save_all()
        suppressed_ids = {int(segment.get("id", 0)) for segment in suppressed}
        targets = [segment for segment in targets if int(segment.get("id", 0)) not in suppressed_ids]
    if not targets:
        await _verify_refinement_candidates(record,config)
        progress=saved_progress(record)
        _update_ai_job(record,"refinement",completed=progress['saved_segments'],total=progress['total_segments'],
            progress_unit='段',saved_segments=progress['saved_segments'],remaining_segments=0)
        return
    context_window = context_window_for(config, "correction")
    output_budget = output_limit_for(config, "correction", 32_768)
    long_context = uses_agy_gemini(config, "correction")
    token_budget = agy_rewrite_source_budget(config, "correction") if long_context else max(
        128,
        min(
            2_400,
            max(128, int(max(128, output_budget - 384) * 0.45)),
            context_safe_source_budget(context_window, fixed_overhead=1_800),
        ),
    )
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for segment in targets:
        cost = estimate_tokens(str(segment.get("raw") or "")) + 24
        if current and (
            current_tokens + cost > token_budget
        ):
            groups.append(current)
            current, current_tokens = [], 0
        current.append(segment)
        current_tokens += cost
    if current:
        groups.append(current)
    progress=saved_progress(record)
    def update_progress():
        progress=saved_progress(record)
        _update_ai_job(record,"refinement",completed=progress['saved_segments'],total=progress['total_segments'],
            progress_unit='段',saved_segments=progress['saved_segments'],remaining_segments=progress['remaining_segments'],
            total_batches=len(groups),completed_batches=completed)
    semaphore = asyncio.Semaphore(concurrency_for(config, "correction"))
    completed = 0
    update_progress()
    async def process_group(index, group):
        nonlocal completed
        async with semaphore:
            group_config = json.loads(json.dumps(config))
            group_config['_preserve_segment_boundaries']=True
            group_config['_transcript_context']=context_for(record,group,context_window)
            from qwen_asr_app.transcription_assist.service import evidence_config
            group_config = await evidence_config(record,group_config,'\n'.join(str(s.get('raw') or '') for s in group),limit=12)
            installed=set()
            def checkpoint(blocks):
                fresh=[b for b in blocks if not set(b['segment_ids'])<=installed]
                if not fresh:return
                commit_blocks(record,fresh)
                installed.update(i for b in fresh for i in b['segment_ids'])
                update_progress()
            mapped = await polish_timestamped_segments_resilient(
                group,str(group[0].get('language') or ''),group_config,mode='correction',on_checkpoint=checkpoint)
            checkpoint(mapped)
            completed += 1
            update_progress()
    workers = [asyncio.create_task(process_group(index, group)) for index, group in enumerate(groups)]
    try:
        # A sibling failure does not discard other independent requests.
        results=await asyncio.gather(*workers,return_exceptions=True)
        failures=[result for result in results if isinstance(result,BaseException)]
        if failures:raise failures[0]
    finally:
        for worker in workers:
            if not worker.done():worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        update_progress()

    await _verify_refinement_candidates(record,config)


async def _verify_refinement_candidates(record,config):
    from qwen_asr_app.transcription_assist.verification import verify_corrections
    await verify_corrections(record,config,on_progress=lambda done,total:
        _update_ai_job(record,'refinement',phase=f'AI 自动复核 · {done}/{total} 段'))


async def resume_correction_verifications():
    """Resume protected edits once on startup, respecting explicit stops."""
    from qwen_asr_app.transcription_assist.routes import active_candidates
    ids=[r.id for r in getattr(recording_store,'iter_records',recording_store.list_records)()
         if r.metadata.get('status')=='completed' and active_candidates(r)]
    for ident in ids:
        await queue_correction_verification(ident)


async def queue_correction_verification(ident):
    record=recording_store.find(ident)
    if record is None:return
    from qwen_asr_app.transcription_assist.routes import active_candidates
    if not active_candidates(record):return
    if _saved_ai_job(record,'refinement').get('status') in {'paused','cancelled'}:return
    # Summary generation checks the same candidates while owning the record.
    if ident in background_summary_tasks:return
    try:
        await ai_task_action({'recording_id':ident,'type':'refinement','action':'continue'})
    except HTTPException as exc:
        _update_ai_job(record,'refinement',status='failed',error=str(exc.detail),error_kind='configuration')



async def _run_refinement_job(record: RecordingRecord, config: dict[str, Any]) -> None:
    live_session = live_sessions.get(record.id)
    if live_session is None:
        await _run_refinement_job_unlocked(record, config)
        return
    async with live_session.review_lock:
        await _run_refinement_job_unlocked(record, config)


def _live_summary_interval(record: RecordingRecord, config: dict[str, Any]) -> int:
    try:
        configured = int(config.get("summary_interval_minutes") or 0) * 60
    except (TypeError, ValueError):
        configured = 0
    if configured >= 60:
        return configured
    durations = [
        int(round(float(item.get("end", 0)) - float(item.get("start", 0))))
        for item in record.metadata.get("ai_summaries") or []
        if isinstance(item, dict) and float(item.get("end", 0)) > float(item.get("start", 0))
    ]
    return max(60, min(durations or [600]))


async def _run_live_summary_job_unlocked(record: RecordingRecord, config: dict[str, Any]) -> None:
    config = {**config, "_live_summary": True}
    summaries, _ = _deduplicated_live_summaries(record)
    interval = _live_summary_interval(record, config)
    failed_ends = [int(round(float(item.get("end", 0)))) for item in summaries if item.get("ai_error")]
    restart_end = min(failed_ends) if failed_ends else None
    if restart_end is not None:
        summaries = [item for item in summaries if int(round(float(item.get("end", 0)))) < restart_end]
    previous_summary = next(
        (str(item.get("summary") or "") for item in reversed(summaries) if item.get("summary") and not item.get("ai_error")),
        "",
    )
    record.metadata["ai_summaries"] = summaries
    pending = due_summary_windows(record.segments, summaries, interval)
    _update_ai_job(record, "live_summary", total=len(summaries) + len(pending), completed=len(summaries))
    for index, (window_start, window_end, window_segments) in enumerate(pending):
        item: dict[str, Any] = {
            "id": f"{window_start}-{window_end}",
            "start": window_start,
            "end": window_end,
            "segment_ids": [int(segment["id"]) for segment in window_segments],
        }
        try:
            blocks = [
                f"[{format_clock(float(segment.get('start', 0)))}–{format_clock(float(segment.get('end', 0)))}]\n"
                f"{segment.get('corrected') or segment.get('raw') or ''}"
                for segment in window_segments
            ]
            chunks = chunk_text_blocks(blocks, context_window_for(config, "review"))
            key_points: list[str] = []
            for chunk in chunks:
                result = await summarize_transcript(chunk, previous_summary, config, provider_mode="review")
                previous_summary = str(result["summary"])
                key_points = list(result.get("key_points") or [])
            item.update(summary=previous_summary, key_points=key_points, parts=len(chunks))
            summaries.append(item)
            record.metadata["ai_summaries"] = summaries
            record.save_all()
            _update_ai_job(record, "live_summary", completed=len(summaries))
        except Exception as exc:
            item.update(ai_error=str(exc), error_kind=getattr(exc, "kind", "upstream"))
            summaries.append(item)
            record.metadata["ai_summaries"] = summaries
            record.save_all()
            raise


async def _run_live_summary_job(record: RecordingRecord, config: dict[str, Any]) -> None:
    live_session = live_sessions.get(record.id)
    if live_session is None:
        await _run_live_summary_job_unlocked(record, config)
        return
    async with live_session.summary_lock:
        await _run_live_summary_job_unlocked(record, config)


@app.post("/api/ai-tasks/action")
async def ai_task_action(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record_id = str(payload.get("recording_id") or "")
    kind = str(payload.get("type") or "")
    action = str(payload.get("action") or "")
    record = recording_store.find(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    if record_id in running_batch_ids:
        raise HTTPException(409, "课程正在转写或补齐，请完成后再操作 AI 任务")
    if kind=='remote_asr':
        if action not in {'pause','cancel','retry','continue'}:raise HTTPException(400,'待补转任务操作无效')
        record.metadata.setdefault('remote_backlog',{}).update(status={'pause':'paused','cancel':'cancelled'}.get(action,'pending'))
        record.save_metadata()
        return {'saved':True,'audio_preserved':True}
    if kind not in AI_TASK_LABELS or action not in {"retry", "regenerate", "continue", "pause", "cancel"}:
        raise HTTPException(status_code=400, detail="AI 任务操作无效")
    if action == "pause":
        if kind == "summary":
            pause_requested_tasks.add(f"summary:{record_id}")
            saved = record.metadata.setdefault("summary_job", {})
            saved["pause_requested"] = True
            saved["updated_at"] = local_now()
            record.save_metadata()
            task = background_summary_tasks.get(record_id)
            if task and not task.done():
                task.cancel()
                return {"paused": True}
            direct = summary_jobs.get(record_id)
            if direct and not direct["task"].done():
                direct["task"].cancel()
                return {"paused": True}
            saved.update(
                status="paused", error="任务已暂停；已完成内容保留，可继续",
                error_kind="paused", retry_in_seconds=0, pause_requested=False,
                updated_at=local_now(),
            )
            record.save_metadata()
            pause_requested_tasks.discard(f"summary:{record_id}")
            return {"paused": True}
        saved = _saved_ai_job(record, kind)
        pause_requested_tasks.add(f"{kind}:{record_id}")
        saved["pause_requested"] = True
        record.save_metadata()
        task = background_ai_tasks.get(f"{kind}:{record_id}")
        if task and not task.done():
            task.cancel()
            return {"paused": True}
        live_session = live_sessions.get(record_id)
        periodic_kind = "review" if kind == "refinement" else "summary"
        periodic = live_session.periodic_tasks.get(periodic_kind) if live_session else None
        if periodic and not periodic.done():
            live_session.periodic_dirty[periodic_kind] = False
            periodic.cancel()
        _update_ai_job(
            record, kind, status="paused", error="任务已暂停；已完成内容保留，可继续",
            error_kind="paused", retry_in_seconds=0, pause_requested=False,
        )
        pause_requested_tasks.discard(f"{kind}:{record_id}")
        return {"paused": True}
    if action == "cancel":
        cancel_requested_tasks.add(f"{kind}:{record_id}")
        pause_requested_tasks.discard(f"{kind}:{record_id}")
        if kind == "summary":
            return await cancel_summary(record_id)
        task = background_ai_tasks.get(f"{kind}:{record_id}")
        if task and not task.done():
            task.cancel()
            return {"cancelled": True}
        live_session = live_sessions.get(record_id)
        periodic_kind = "review" if kind == "refinement" else "summary"
        periodic = live_session.periodic_tasks.get(periodic_kind) if live_session else None
        if periodic and not periodic.done():
            live_session.periodic_dirty[periodic_kind] = False
            periodic.cancel()
            _update_ai_job(
                record,
                kind,
                status="cancelled",
                error="任务已取消；已完成内容保留，可继续",
                error_kind="cancelled",
            )
            return {"cancelled": True}
        saved = _saved_ai_job(record, kind)
        if saved.get("status") in {"waiting_retry", "paused", "failed", "interrupted", "pending"} or (
            kind == "live_summary" and any(
                isinstance(item, dict) and item.get("ai_error")
                for item in record.metadata.get("ai_summaries") or []
            )
        ) or (kind == "refinement" and _unrefined_segments(record)):
            _update_ai_job(
                record,
                kind,
                status="cancelled",
                error="任务已取消；已完成内容仍然保留",
                error_kind="cancelled",
            )
            return {"cancelled": True}
        raise HTTPException(status_code=409, detail="任务当前没有运行")
    # Check before clearing drafts or saved reviews. Repeated clicks on an
    # already-running task are harmless, including during automatic backoff.
    existing = (background_summary_tasks.get(record_id) if kind == "summary"
                else background_ai_tasks.get(f"{kind}:{record_id}"))
    if existing is not None and not existing.done():
        return {"accepted": False}
    require_recording_available(record_id)
    try:
        # Ignore browser-supplied provider/model/key fields completely. The
        # durable local settings and endpoint-bound secret store are the sole
        # source of truth for a retry or continuation.
        config = _saved_ai_task_config(record, kind)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if kind == "refinement" and record.metadata.get("status") in {"active", "processing"}:
        raise HTTPException(status_code=409, detail="请先结束当前录音，再重新精修尚未完成的段落")
    course = record.metadata.get("course_settings") or {}
    config.setdefault("terminology", course.get("terminology") or [])
    if kind == "refinement":
        config["_parallel_refinement"] = True
    resume_abstract = kind == "summary" and action != "regenerate" and _summary_abstract_pending(record)
    if action == "regenerate" and kind == "summary":
        # Maintenance regeneration deliberately uses bounded detailed chunks;
        # a single very long Gemini response can otherwise hit its output cap
        # before the complete lecture note is returned.
        config["_summary_force_chunk_tokens"] = 6000
    if action == "regenerate":
        # Batch maintenance keeps the saved Gemini High model but uses a
        # bounded thinking level so one stale provider request cannot hold the
        # whole repair batch indefinitely. This does not change daily settings.
        ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
        for provider in ai.values():
            if isinstance(provider, dict) and str(provider.get("transport") or "").lower() == "agy_cli":
                provider["cli_model"] = "gemini-3.8-flash-high"
                provider["model"] = "gemini-3.8-flash-high"
                provider["cli_effort"] = "high"
                if kind == "refinement":
                    provider["concurrency"] = max(4, int(provider.get("concurrency") or 0))
    if action in {"retry", "regenerate"}:
        if kind == "summary":
            if not resume_abstract:
                record.metadata.pop("ai_note_summary_draft", None)
        elif kind == "live_summary":
            record.metadata["ai_summaries"] = []
        else:
            if action == "regenerate":
                # Maintenance regeneration must actually use the selected
                # provider for every segment. Keep raw ASR text and discard
                # only derived correction/global-review outputs.
                from qwen_asr_app.durable import atomic_json
                atomic_json(record.directory/'.refinement-history'/('before-regenerate-'+str(time.time_ns())+'.json'),
                    {'metadata':record.metadata,'segments':record.segments})
                config['_force_refinement']=True
                record.metadata['refinement_epoch']=str(time.time_ns())
                record.metadata["ai_reviews"] = []
                record.metadata.pop("ai_global_rewrite", None)
                for segment in record.segments:
                    segment.pop("corrected", None)
                    segment.pop("translation", None)
                    segment.pop("correction_review",None)
                    segment.pop("ai_error", None)
                    segment["ai_pending"] = True
            # Retry/continue preserve all validated results and candidates.
        record.save_all()
    if kind == "summary":
        summary_payload = {
            "config": config,
            "_resume_abstract": resume_abstract,
            "_pdf_only": (record.metadata.get('summary_job') or {}).get('resume_stage') == 'pdf',
            "summary_system_prompt": config.get("_note_summary_system_prompt") or DEFAULT_NOTE_SUMMARY_PROMPT,
            "abstract_system_prompt": config.get("_note_abstract_system_prompt") or DEFAULT_NOTE_ABSTRACT_PROMPT,
        }
        start_summary_job(record, summary_payload)
        return {"accepted": True}
    if kind == "refinement":
        job_runner = lambda: _run_refinement_job(record, config)
    else:
        job_runner = lambda: _run_live_summary_job(record, config)
    accepted = _start_aux_ai_job(record, kind, job_runner)
    return {"accepted": accepted}


@app.post("/api/ai-tasks/bulk-action")
async def bulk_ai_task_action(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    action = str(payload.get("action") or "")
    items = payload.get("items")
    if action not in {"continue", "pause", "retry"} or not isinstance(items, list):
        raise HTTPException(status_code=400, detail="批量 AI 任务操作无效")
    if len(items) > 200:
        raise HTTPException(status_code=400, detail="单次最多处理 200 个 AI 任务")
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        request = {**item, "action": action}
        try:
            result = await ai_task_action(request)
            results.append({
                "recording_id": str(item.get("recording_id") or ""),
                "type": str(item.get("type") or ""),
                "ok": True,
                "result": result,
            })
        except HTTPException as exc:
            results.append({
                "recording_id": str(item.get("recording_id") or ""),
                "type": str(item.get("type") or ""),
                "ok": False,
                "error": str(exc.detail),
            })
    return {
        "requested": len(items),
        "accepted": sum(bool(item.get("ok")) for item in results),
        "failed": sum(not bool(item.get("ok")) for item in results),
        "results": results,
    }


service_draining = False
service_mutations = 0


@app.middleware("http")
async def service_guard(request: Request, call_next):
    global service_mutations
    if os.getenv('QWEN_ASR_PREVIEW') == '1' and '/ideashell/' in request.url.path and request.method not in {'GET','HEAD','OPTIONS'} and request.url.path.rsplit('/',1)[-1] in {'push','pull','reconcile'}:
        return JSONResponse({'detail':'这是隔离测试页，外部笔记同步已停用。请使用原来的 http://127.0.0.1:8765/ 正式页面。'},status_code=409)
    if not http_request_allowed(request, os.getenv("QWEN_ASR_ALLOWED_ORIGINS", ""), testing=bool(os.getenv("QWEN_ASR_DATA_DIR"))):
        return JSONResponse({"detail": "请求来源不受信任，请从本机工作台操作"}, status_code=403)
    if service_draining and request.method not in {"GET", "HEAD"} and not request.url.path.startswith("/api/service/"):
        return JSONResponse({"detail": "服务正在安全退出，请重新启动后继续"}, status_code=503)
    if backup_manager.busy and request.method not in {"GET", "HEAD", "OPTIONS"}:
        return JSONResponse({"detail": "日常备份正在打包，请完成后再启动任务或修改配置"}, status_code=409)
    target = re.match(r'^/api/recordings/([^/]+)(?:/|$)', request.url.path)
    if target:
        # A manually restored recording keeps its identity, but a completed
        # discard receipt must no longer fence its archived files forever.
        recording_discard.reconcile_restored(target.group(1))
    if (target and request.method not in {'GET', 'HEAD', 'OPTIONS'}
            and not request.url.path.endswith('/discard')
            and recording_discard.busy(target.group(1))):
        return JSONResponse({'detail': '这条录音正在安全丢弃，请等待完成'}, status_code=409)
    tracked = request.method not in {"GET", "HEAD", "OPTIONS"} and not request.url.path.startswith("/api/service/")
    if tracked:
        service_mutations += 1
    try:
        return await call_next(request)
    finally:
        if tracked:
            service_mutations -= 1


@app.post("/api/service/{action}")
async def service_stop_guard(action: str, request: Request):
    global service_draining
    token = os.getenv("QWEN_ASR_INSTANCE")
    if not token or request.headers.get("X-QwenASR-Control") != token:
        raise HTTPException(403, "请使用本机停止服务启动器")
    if action == "cancel-stop":
        service_draining = False
        library_watch.draining = False
        return {"ready": False}
    if action != "prepare-stop":
        raise HTTPException(404, "操作不存在")
    active = recording_store.active
    tasks = [
        *background_summary_tasks.values(),
        *background_ai_tasks.values(),
        *finalization_tasks.values(),
        *recording_discard.tasks.values(),
        *pdf_ai_background_tasks.values(),
        *course_file_api.tasks.values(),
        *upload_jobs.tasks.values(),
        *intelligence_router.intelligence_service._running_tasks.values(),
        *intelligence_router.intelligence_service._debounce_tasks.values(),
    ]
    if (backup_manager.busy or backup_manager.downloads or service_mutations or model_manager.active_jobs or model_manager.connected_clients or running_batch_ids or record_operations
        or knowledge_service.job_manager._active_job_id or knowledge_service.job_manager._queue.qsize()
        or any(not t.done() for t in tasks) or (active and active.metadata.get("status") == "active")):
        raise HTTPException(409, "录音或任务仍在运行，进度已保留，请完成或暂停后再停止")
    service_draining = True
    library_watch.draining = True
    library_watch.publish()
    return {"ready": True}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    from qwen_asr_app.inference_providers import inference_settings
    from qwen_asr_app.runtime_memory import rss_bytes,last_reclaim
    try:
        mode = inference_settings.for_task('asr')['asr_provider']
    except (OSError, ValueError):
        mode = 'unknown'
    return {
        "service": "workbench", "pid": os.getpid(), "instance": os.getenv("QWEN_ASR_INSTANCE", ""),
        "status": model_manager.status,
        "detail": model_manager.detail,
        "device": model_manager.device,
        "load_seconds": model_manager.load_seconds,
        "performance": model_manager.performance(),
        "model": "Qwen3-ASR-1.7B BF16 (MLX)",
        "model_path": str(MODEL_PATH),
        "asr_provider": mode,
        "local_model_loaded": bool(model_manager.worker and model_manager.worker.is_alive()),
        "memory":{'rss_bytes':rss_bytes(),'last_reclaim':dict(last_reclaim)},
    }


@app.get("/api/config")
async def public_config() -> dict[str, Any]:
    saved_keys = ai_secret_store.status()
    return {
        "feature_version": "library-upgrade-v15-classroom-chat",
        "knowledge_feature_version": "course-knowledge-v1",
        "preview": os.getenv("QWEN_ASR_PREVIEW") == "1",
        "ai_base_url": os.getenv("AI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        "ai_model": os.getenv("AI_MODEL", "gpt-5-mini"),
        "ai_review_base_url": os.getenv("AI_REVIEW_BASE_URL") or os.getenv("AI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        "ai_review_model": os.getenv("AI_REVIEW_MODEL") or os.getenv("AI_MODEL", "gpt-5-mini"),
        "has_review_api_key": bool(saved_keys["review"] or os.getenv("AI_REVIEW_API_KEY") or os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY")),
        "ai_summary_base_url": os.getenv("AI_SUMMARY_BASE_URL") or os.getenv("AI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        "ai_summary_model": os.getenv("AI_SUMMARY_MODEL") or os.getenv("AI_MODEL", "gpt-5-mini"),
        "has_summary_api_key": bool(saved_keys["summary"] or os.getenv("AI_SUMMARY_API_KEY") or os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY")),
        "default_context_window": DEFAULT_CONTEXT_WINDOW,
        "has_api_key": bool(saved_keys["sentence"] or os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY")),
        "saved_provider_keys": saved_keys,
        "default_asr_params": DEFAULT_ASR_PARAMS,
        "default_live_params": DEFAULT_LIVE_PARAMS,
        "default_ai_system_prompt": DEFAULT_AI_SYSTEM_PROMPT,
        "default_ai_title_prompt": DEFAULT_AI_TITLE_PROMPT,
        "default_note_summary_prompt": DEFAULT_NOTE_SUMMARY_PROMPT,
        "default_note_abstract_prompt": DEFAULT_NOTE_ABSTRACT_PROMPT,
    }


@app.get("/api/library")
async def recording_library(request: Request = None, category: str = '', date: str = '',
                            offset: int = 0, limit: int = 0, include_settings: bool = True, ids: str = ''):
    from qwen_asr_app.library_query import listing, conditional
    if offset < 0 or not 0 <= limit <= 100 or (date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date)):
        raise HTTPException(400, '分页或日期参数无效')
    discover_courses(app_settings)
    visit=getattr(recording_store,'iter_records',recording_store.list_records)
    for record in visit():recording_discard.reconcile_restored(record.id)
    records=visit()
    if ids:
        wanted = ids.split(',')
        if len(wanted) > 100 or any(not re.fullmatch(r'[A-Za-z0-9_-]{6,80}', value) for value in wanted):
            raise HTTPException(400, '录音编号列表无效')
        records = [record for record in records if record.id in set(wanted)]
    payload = {**(app_settings.public() if include_settings else {'categories': app_settings.categories}),
               **listing(records, category, date, offset, limit)}
    # Legacy callers without pagination still get all records, as small DTOs.
    return conditional(payload, request) if request is not None else payload


@app.get("/api/library/events")
async def library_events(request: Request):
    if service_draining:
        raise HTTPException(503, '服务正在退出')
    return StreamingResponse(library_watch.events(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.put("/api/library/settings")
async def update_library_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        requested_root = str(payload.get("storage_root") or "")
        requested_path = Path(requested_root).expanduser()
        same_root = requested_path.is_absolute() and requested_path.resolve() == app_settings.storage_root.resolve()
        if not same_root:
            tasks = [
                *background_summary_tasks.values(), *background_ai_tasks.values(),
                *finalization_tasks.values(), *pdf_ai_background_tasks.values(),
                *course_file_api.tasks.values(),
        *upload_jobs.tasks.values(),
            ]
            # This request accounts for one service_mutations entry. Uploads
            # can already hold paths before registering a recording/task ID.
            if (record_operations or running_batch_ids or model_manager.active_jobs
                    or service_mutations > 1 or course_file_api.lock.locked() or pdf_ai_lock.locked()
                    or any(not task.done() for task in tasks)
                    or knowledge_service.job_manager._active_job_id or knowledge_service.job_manager._queue.qsize()
                    or any(record.metadata.get("status") in {"active", "processing"}
                           for record in recording_store.list_records())):
                raise ValueError("录音或后台任务仍在处理，请完成或暂停后再修改资料库位置")
        # Keep the availability check and root change synchronous: no task can
        # start with the old paths between this check and the store refresh.
        migrate = payload.get("migrate", True) is not False
        migration: dict[str, Any] = {
            "source_root": str(app_settings.storage_root),
            "storage_root": str(app_settings.storage_root),
            "migrated_records": 0,
            "migrated_files": 0,
            "skipped": [],
        }
        if migrate:
            migration = app_settings.migrate_storage_root(requested_root)
        else:
            app_settings.set_storage_root(requested_root)
        raw_categories = payload.get("categories")
        if isinstance(raw_categories, list):
            for category in raw_categories:
                app_settings.add_category(str(category))
        recording_store.refresh()
        if not same_root:
            # Idle course jobs retain RecordingRecord paths too. Reload them
            # from the new library on the next request instead of reusing them.
            course_stream_manager.jobs.clear()
            for session in live_sessions.values():
                session.recording = None
            live_sessions.clear()
        return {**app_settings.public(), "migration": migration}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/library/choose-root")
async def choose_library_root() -> dict[str, Any]:
    """Open the native system folder picker for the local web app."""
    if sys.platform != "darwin":
        raise HTTPException(status_code=400, detail="当前系统没有可用的原生文件夹选择器，请直接填写绝对路径")
    default_path = str(app_settings.storage_root).replace('\\', '\\\\').replace('"', '\\"')
    script = (
        'POSIX path of (choose folder with prompt "选择 QwenASR 资料库位置" '
        f'default location POSIX file "{default_path}")'
    )
    result = await asyncio.to_thread(
        subprocess.run,
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"cancelled": True}
    selected = result.stdout.strip()
    if not selected:
        return {"cancelled": True}
    return {"cancelled": False, "storage_root": str(Path(selected).expanduser().resolve())}


def _pdf_ai_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return pdf_jobs.snapshot(job)


@app.get("/api/pdf-ai/choose-files")
async def choose_pdf_ai_files() -> dict[str, Any]:
    """Open Finder's native PDF picker for this local macOS application."""
    if sys.platform != "darwin":
        raise HTTPException(status_code=400, detail="当前系统没有可用的原生 PDF 选择器")
    script = """
set chosenFiles to choose file with prompt "选择需要生成 AI 友好版本的 PDF" of type {"com.adobe.pdf"} with multiple selections allowed
set outputText to ""
repeat with chosenFile in chosenFiles
    set outputText to outputText & POSIX path of chosenFile & linefeed
end repeat
return outputText
"""
    result = await asyncio.to_thread(
        subprocess.run,
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"cancelled": True, "paths": []}
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {"cancelled": not paths, "paths": paths}


@app.get("/api/pdf-ai/jobs/current")
async def current_pdf_ai_job() -> dict[str, Any]:
    if not pdf_ai_jobs:
        return {"job": None, "mineru_available": find_mineru_binary() is not None}
    latest = max(pdf_ai_jobs.values(), key=lambda item: item.get("created_at", 0))
    return {
        "job": _pdf_ai_job_snapshot(latest),
        "mineru_available": find_mineru_binary() is not None,
    }


@app.get("/api/pdf-ai/jobs/{job_id}")
async def pdf_ai_job_status(job_id: str) -> dict[str, Any]:
    job = pdf_ai_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="没有找到这个 PDF 转换任务")
    return _pdf_ai_job_snapshot(job)


@app.post("/api/pdf-ai/jobs")
async def start_pdf_ai_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return pdf_jobs.start(payload.get("paths"))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/library/categories")
async def create_library_category(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分类名称不能为空")
    return {"category": app_settings.add_category(name), **app_settings.public()}


def change_library_category(category: str, name: str | None = None) -> dict[str, Any]:
    affected = {category, "未分类" if name is None else name}
    records = [record for record in recording_store.list_records()
               if record.metadata.get("category") in affected]
    if (service_mutations > 1 or course_file_api.lock.locked() or model_manager.active_jobs
            or any(not task.done() for task in pdf_ai_background_tasks.values())
            or any(key[0] in affected and not task.done() for key, task in course_file_api.tasks.items())
            or any(recording_is_busy(record.id) or record.metadata.get("status") in {"active", "processing"}
                   for record in records)):
        raise HTTPException(409, "资料或录音仍在处理，请完成或暂停相关任务后再修改分类")
    # Do not yield between this check and the moves: background work must not
    # acquire a category path halfway through changing it.
    review_changed = False
    previous_review_state = copy.deepcopy(review_library.state)
    try:
        review_changed = review_library.reclassify(category, "未分类" if name is None else name)
        result = change_category(app_settings, recording_store, category, name)
    except (ValueError, OSError, RuntimeError) as exc:
        if review_changed:
            review_library.state = previous_review_state
            review_library._save()
        raise HTTPException(400, str(exc)) from exc
    for record in records:
        course_stream_manager.jobs.pop(record.id, None)
        session = live_sessions.pop(record.id, None)
        if session is not None:
            session.recording = None
    course_file_api.previews = {
        key: preview for key, preview in course_file_api.previews.items()
        if preview.get("category") not in affected
    }
    return {**app_settings.public(), **result}


@app.post("/api/library/categories/rename")
async def rename_library_category(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    course_file_api.guard(request)
    return change_library_category(str(payload.get("category") or ""), str(payload.get("name") or "").strip())


@app.post("/api/library/categories/delete")
async def delete_library_category(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    course_file_api.guard(request)
    return change_library_category(str(payload.get("category") or ""))


@app.put("/api/library/category-profile")
async def update_category_profile(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    category = str(payload.get("category") or "").strip()
    if not category:
        raise HTTPException(status_code=400, detail="请先填写课程分类")
    try:
        profile = app_settings.save_category_profile(category, payload)
        return {
            "category": safe_component(category, "未分类"),
            "profile": profile,
            **app_settings.public(),
        }
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/recordings/{session_id}")
@recording_operation
async def update_recording(session_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    title = str(payload.get("title") or record.metadata.get("title") or "").strip()
    category = str(payload.get("category") or record.metadata.get("category") or "未分类")
    try:
        updated = recording_store.update(session_id, title, category)
        return {"session": updated.public(), **app_settings.public()}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def apply_recording_title(record: RecordingRecord, title: str) -> RecordingRecord:
    updated = recording_store.update(record.id, title, str(record.metadata.get("category") or "未分类"))
    summary = updated.metadata.get("ai_note_summary")
    if isinstance(summary, dict) and "title_error" in summary:
        summary.pop("title_error")
        updated.save_metadata()
    return updated


@app.post("/api/recordings/{session_id}/generate-title")
@recording_operation
async def generate_title(session_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    note_summary = record.metadata.get("ai_note_summary")
    summary = (
        str(note_summary.get("summary") or "").strip()
        if isinstance(note_summary, dict)
        else ""
    )
    if not summary:
        raise HTTPException(status_code=400, detail="请先为这条录音生成详细 AI 总结")
    try:
        config = ai_preferences.task_config(payload.get("config"), course=record.metadata.get("course_settings") or {}, required_modes=("summary",))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    settings = ai_preferences.read()
    prompt = str(settings.get("title_prompt") or DEFAULT_AI_TITLE_PROMPT).strip()
    try:
        title = await generate_recording_title(
            summary,
            prompt,
            config,
            current_title=str(record.metadata.get("title") or ""),
            category=str(record.metadata.get("category") or ""),
        )
        result = {"title": title, "summary": summary, "summary_source": "ai"}
        if payload.get("apply") is True:
            updated = apply_recording_title(record, title)
            result["session"] = updated.public(include_segments=False)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 标题请求失败：{exc}") from exc


@app.post("/api/recordings/{session_id}/generate-summary")
@recording_operation
async def generate_library_summary(
    session_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    if record.metadata.get("status") != "completed":
        raise HTTPException(status_code=400, detail="请等待录音或转写完整结束后再生成详细 AI 总结")
    if not any(
        str(item.get("corrected") or item.get("raw") or "").strip()
        for item in record.segments
    ):
        raise HTTPException(status_code=400, detail="录音还没有可用于生成详细 AI 总结的转写内容")
    try:
        config = ai_preferences.task_config(payload.get("config"), course=record.metadata.get("course_settings") or {}, required_modes=("summary",))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    config["_note_summary_system_prompt"] = str(
        config.get("_note_summary_system_prompt") or DEFAULT_NOTE_SUMMARY_PROMPT
    ).strip()[:30_000]
    config["_note_summary_custom_prompt"] = str(
        payload.get("custom_prompt") or ""
    ).strip()[:20_000]
    config["_note_abstract_system_prompt"] = str(
        config.get("_note_abstract_system_prompt") or DEFAULT_NOTE_ABSTRACT_PROMPT
    ).strip()[:20_000]
    try:
        summary = await build_note_summary(record, config)
        # Keep naming inside the server-owned operation, after the note is saved.
        # Its failure must not discard the note or restart expensive generation.
        job = summary_jobs.get(session_id)
        if job is not None:
            job["phase"] = "AI 取名"
        try:
            prompt = str(payload.get("title_prompt") or ai_preferences.read().get("title_prompt")
                         or DEFAULT_AI_TITLE_PROMPT).strip()
            title = await generate_recording_title(
                summary["summary"], prompt, config,
                current_title=str(record.metadata.get("title") or ""),
                category=str(record.metadata.get("category") or ""),
            )
            # update() can move the directory and returns a fresh record instance.
            record = apply_recording_title(record, title)
            summary = record.metadata["ai_note_summary"]
        except Exception as exc:
            record = recording_store.find(session_id) or record
            summary = record.metadata["ai_note_summary"]
            summary["title_error"] = str(exc)
            record.save_metadata()
        return {"summary": summary, "session": record.public(include_segments=False)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 总结请求失败：{exc}") from exc


def start_summary_job(record: RecordingRecord, payload: dict[str, Any], *, automatic: bool = False) -> None:
    """Own generation on the server so losing the browser cannot cancel it."""
    session_id = record.id
    if session_id in background_summary_tasks or session_id in summary_jobs:
        return
    require_recording_available(session_id, owner=asyncio.current_task())
    try:
        _remember_ai_keys(payload.get("config"))
        payload = {**payload, "config": ai_preferences.task_config(
            payload.get("config"), course=record.metadata.get("course_settings") or {},
            required_modes=("summary",),
        )}
    except (OSError, ValueError) as exc:
        if os.getenv("QWEN_ASR_TESTING") != "1":
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    cancel_requested_tasks.discard(f"summary:{session_id}")
    resume_abstract = bool(payload.get("_resume_abstract")) and _summary_abstract_pending(record)
    # Only progress is persisted; credentials remain in this task's memory.
    previous = record.metadata.get("summary_job") or {}
    previous_error = previous.get("error") or previous.get("previous_error") or ""
    record.metadata["summary_job"] = {
        "status": "queued", "automatic": automatic, "started_at": local_now(), "error": "",
        "auto_retry_count": 0, "retry_in_seconds": 0, "pause_requested": False,
        "resume_stage": "pdf" if payload.get('_pdf_only') else "abstract" if resume_abstract else "",
        "previous_error": previous_error,
    }
    record.save_metadata()

    async def execute() -> None:
        automatic_attempt = 0

        def save_state(status: str, error: str = "", **values: Any) -> None:
            current = recording_store.find(session_id)
            if current is not None:
                current.metadata.setdefault("summary_job", {}).update(
                    status=status, error=error, updated_at=local_now(), **values)
                current.save_metadata()
        try:
            while True:
                try:
                    save_state("running", retry_in_seconds=0)
                    result = await (upgrade_library_note_pdf(session_id, payload) if payload.get('_pdf_only') else generate_library_summary_abstract(session_id, payload) if resume_abstract
                                    else generate_library_summary(session_id, payload))
                    note_result = result.get('summary') or {}
                    if note_result.get('review_status') in {'needs_revision','unreviewed'}:
                        save_state('completed_with_errors',note_result.get('review_warning') or '候选笔记已保存，独立审查仍有待处理项',resume_stage='pdf')
                        return
                    abstract_error = (result.get("summary") or {}).get("abstract_error")
                    if abstract_error:
                        save_state("completed_with_errors", str(abstract_error), resume_stage="abstract",
                                   retry_in_seconds=0, auto_retry_count=automatic_attempt)
                        return
                    save_state(
                        "completed", auto_retry_count=automatic_attempt,
                        retry_in_seconds=0, resume_stage="",
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                    error_kind = _error_kind(exc)
                    automatic_attempt += 1
                    if error_kind in AUTO_RETRY_KINDS and automatic_attempt <= MAX_AUTOMATIC_RETRIES:
                        delay = _automatic_retry_delay(automatic_attempt)
                        save_state(
                            "waiting_retry", error, error_kind=error_kind,
                            auto_retry_count=automatic_attempt, retry_in_seconds=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    save_state(
                        "interrupted" if isinstance(exc, HTTPException) and exc.status_code == 409 else "failed",
                        error, error_kind=error_kind, auto_retry_count=automatic_attempt,
                        retry_in_seconds=0,
                    )
                    return
        except asyncio.CancelledError:
            current = recording_store.find(session_id)
            job = current.metadata.get("summary_job") if current is not None else {}
            task_id = f"summary:{session_id}"
            paused = task_id in pause_requested_tasks or (
                isinstance(job, dict) and bool(job.get("pause_requested"))
            )
            pause_requested_tasks.discard(task_id)
            save_state(
                "paused" if paused else "cancelled" if task_id in cancel_requested_tasks else "interrupted",
                "生成已暂停，已完成内容保留，可以继续" if paused else "生成已中断，已完成内容保留，可以重新生成",
                error_kind="paused" if paused else "cancelled",
                retry_in_seconds=0,
                pause_requested=False,
            )
            raise
        finally:
            background_summary_tasks.pop(session_id, None)
            schedule_memory_cleanup()

    async def run() -> None:
        with track_ai_activity(f"summary:{session_id}"):
            await execute()

    task = asyncio.create_task(run())
    background_summary_tasks[session_id] = task

    def cleanup(done: asyncio.Task[Any]) -> None:
        # Cancellation before run() gets its first turn bypasses its finally.
        if background_summary_tasks.get(session_id) is done:
            background_summary_tasks.pop(session_id, None)
            if done.cancelled():
                current = recording_store.find(session_id)
                if current is not None:
                    job = current.metadata.setdefault("summary_job", {})
                    task_id = f"summary:{session_id}"
                    paused = task_id in pause_requested_tasks or bool(job.get("pause_requested"))
                    pause_requested_tasks.discard(task_id)
                    job.update(
                        status="paused" if paused else "cancelled" if task_id in cancel_requested_tasks else "interrupted",
                        error="生成已暂停，可以继续" if paused else "生成已取消，可以重新生成",
                        error_kind="paused" if paused else "cancelled",
                        pause_requested=False,
                        retry_in_seconds=0,
                        updated_at=local_now(),
                    )
                    current.save_metadata()
    task.add_done_callback(cleanup)


@app.post("/api/recordings/{session_id}/summary-jobs")
async def create_summary_job(session_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    if record.metadata.get("status") != "completed":
        raise HTTPException(status_code=409, detail="请先结束并保存录音")
    if not isinstance(payload.get("config"), dict):
        raise HTTPException(status_code=400, detail="AI 配置格式无效")
    start_summary_job(record, payload)
    return {"accepted": True, "session_id": session_id}


async def begin_recording_end(session: TranscriptionSession, summary_payload: dict[str, Any]) -> None:
    record = session.recording or recording_store.active
    if record is None:
        raise ValueError("没有需要结束的录音")
    if record.id in finalization_tasks:
        await session.send({"type": "ending", "session": record.public()})
        return
    session.recording = record
    session.accepting_audio = False
    record.metadata["finalization"] = {"status": "running", "started_at": local_now(), "error": ""}
    # Persist intent before waiting for ASR. If the service stops, the browser
    # can resubmit the current provider settings without storing any API keys.
    record.metadata["summary_job"] = {"status": "queued", "automatic": True, "error": ""}
    record.save_metadata()

    async def finish() -> None:
        try:
            # Register finalization before the first await so other requests
            # cannot start writes while the microphone session is draining.
            await session.pause(send_status=False)
            if record.metadata.get("pending_segments") and not session.tasks:
                session.recovered = False
            await session.recover_pending()
            await session.drain(include_ai=False)
            # Final utterances can arrive after the pause lease is revoked.
            # Release again only after they finish, so replay jobs are not
            # left waiting for a stale 90-second classroom reservation.
            await session.release_remote_compute()
            await session.suspend_ai_for_archive()
            # Suspend optional manual AI work before the archive writes.
            auxiliary = [task for key, task in background_ai_tasks.items()
                         if key.endswith(":" + record.id) and not task.done()]
            if auxiliary:
                for task in auxiliary:
                    task.cancel()
                await asyncio.gather(*auxiliary, return_exceptions=True)
            remote_pending = bool(record.metadata.get('pending_segments')) and all(
                (p.get('config_snapshot') or {}).get('inference_settings',{}).get('asr_provider')=='remote_only'
                for p in record.metadata.get('pending_segments',{}).values())
            if record.metadata.get("pending_segments") and not remote_pending:
                raise RuntimeError("部分录音尚未成功转写，音频已保留，请重试结束保存")
            completion = asyncio.create_task(asyncio.to_thread(record.complete))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(completion)
                    break
                except asyncio.CancelledError:
                    if completion.cancelled():
                        raise
                    # Even repeated cancellation must retain ownership until
                    # the encoder's disk writes have finished.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            record.metadata["finalization"].update(status="completed", updated_at=local_now())
            record.save_metadata()
            if recording_store.active is record:
                recording_store.active = None
            await session.send({"type": "ended", "session": record.public()})
            session.recording = None
            live_sessions.pop(record.id, None)
            if remote_pending:
                record.metadata['remote_backlog']={'status':'pending','count':len(record.metadata['pending_segments']),
                    'message':'录音已归档，Windows 待处理片段将在连接恢复后补转'}
                record.save_metadata()
                return
            summary_intent = record.metadata.get("summary_job") or {}
            if not summary_intent.get("cancel_requested"):
                # No await after scheduling: release finalization ownership
                # before the new summary task can enter its operation guard.
                try:
                    start_summary_job(record, summary_payload, automatic=True)
                except Exception as exc:
                    # Archival is already complete. Keep a retryable summary
                    # receipt without changing the successful save outcome.
                    record.metadata['summary_job'] = {
                        'status': 'failed', 'automatic': True,
                        'error': '录音已保存；总结未提交，请保存 AI 设置后重试：' + str(getattr(exc, 'detail', exc)),
                        'error_kind': 'configuration', 'updated_at': local_now(),
                    }
                    record.save_metadata()
            else:
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.create_task(queue_correction_verification(record.id)))
        except asyncio.CancelledError:
            record.metadata["finalization"].update(status="interrupted", error="服务已关闭，请继续结束保存")
            record.save_metadata()
            raise
        except Exception as exc:
            record.metadata["finalization"].update(status="failed", error=str(exc))
            record.save_metadata()
            await session.send({"type": "end_error", "message": str(exc)})
        finally:
            finalization_tasks.pop(record.id, None)

    task = asyncio.create_task(finish())
    finalization_tasks[record.id] = task

    def cleanup(done: asyncio.Task[Any]) -> None:
        if finalization_tasks.get(record.id) is not done:
            return
        finalization_tasks.pop(record.id, None)
        if done.cancelled():
            record.metadata["finalization"].update(status="interrupted", error="保存已中断，请继续结束保存")
            record.save_metadata()

    task.add_done_callback(cleanup)
    await session.send({"type": "ending", "session": record.public()})


@app.post("/api/recordings/{session_id}/generate-summary-abstract")
@recording_operation
async def generate_library_summary_abstract(
    session_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Retry only the short library abstract while preserving the full Markdown note."""
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    try:
        config = ai_preferences.task_config(payload.get("config"), course=record.metadata.get("course_settings") or {}, required_modes=("summary",))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    config["_note_abstract_system_prompt"] = str(
        config.get("_note_abstract_system_prompt") or DEFAULT_NOTE_ABSTRACT_PROMPT
    ).strip()[:20_000]
    completes_saved_job = _summary_abstract_pending(record)
    try:
        summary = await regenerate_note_abstract(record, config)
        if completes_saved_job:
            record.metadata.pop("ai_note_summary_draft", None)
            record.metadata.setdefault("summary_job", {}).update(
                status="completed", error="", error_kind="", resume_stage="",
                retry_in_seconds=0, updated_at=local_now(),
            )
            record.save_metadata()
        return {"summary": summary, "session": record.public(include_segments=False)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 录音库短摘要请求失败：{exc}") from exc


@app.put("/api/recordings/{session_id}/summary")
@recording_operation
async def save_recording_summary(
    session_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    if record.metadata.get("summary_job", {}).get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="AI 总结仍在生成，草稿已保留，请生成完成后重试保存")
    current = str((record.metadata.get("ai_note_summary") or {}).get("summary") or "")
    if "expected_summary" in payload and str(payload["expected_summary"]).strip() != current.strip():
        # An acknowledged request may have lost its response; the same content is safe.
        if str(payload.get("summary") or "").strip() != current.strip():
            return JSONResponse(status_code=409, content={
                "detail": "这份总结已在其他页面或任务中更新，请核对后再保存当前版本",
                "current_summary": current,
            })
    try:
        summary = record.update_note_summary(str(payload.get("summary") or ""))
        return {"summary": summary, "session": record.public(include_segments=False)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def nested_string(payload: Any, keys: set[str]) -> str:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = nested_string(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = nested_string(value, keys)
            if found:
                return found
    return ""


def ideashell_note_id(payload: dict[str, Any]) -> str:
    value = nested_string(payload, {"note_id", "noteId", "id"})
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        text_value = str(payload.get("text") or "")
        match = re.search(r"(?:note_id|noteId):\s*([a-f0-9]{32})", text_value, re.I)
        value = match.group(1) if match else ""
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise RuntimeError("IdeaShell 返回结果中没有有效的 note_id")
    return value


def ideashell_folder_id(payload: dict[str, Any]) -> str:
    value = nested_string(payload, {"folder_id", "folderId", "id"})
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        text_value = str(payload.get("text") or "")
        match = re.search(r"folder_id:\s*([a-f0-9]{32})", text_value, re.I)
        value = match.group(1) if match else ""
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise RuntimeError("IdeaShell 返回结果中没有有效的 folder_id")
    return value


def ideashell_note_content(payload: dict[str, Any]) -> str:
    value = nested_string(payload, {"content", "body", "markdown", "text"})
    if not value:
        raise RuntimeError("IdeaShell 笔记没有可拉取的 Markdown 正文")
    # Current MCP brief responses contain only title/summary. Full responses
    # expose the current body as Memo 1, followed by retained older memos.
    # Pull only the primary body, never the older half-note or attachment text.
    primary = re.search(r"^\*\*Memo 1:[^\n]*\*\*\s*\n(?:summary:[^\n]*\n)?", value, re.M)
    if primary and "\n## Memos\n" in value:
        value = value[primary.end():]
        boundary = re.search(r"^(?:\*\*Memo \d+:[^\n]*\*\*|## (?:Audios|Links|Images|Documents))\s*$", value, re.M)
        if boundary:
            value = value[:boundary.start()]
        value = value.strip()
        if not value:
            raise RuntimeError("IdeaShell 笔记主正文为空，本地笔记未被覆盖")
    return value


def ideashell_error_text(exc: BaseException) -> str:
    """Expose the useful leaf error from MCP/AnyIO ExceptionGroups."""
    if isinstance(exc, BaseExceptionGroup):
        children = [ideashell_error_text(item) for item in exc.exceptions]
        children = [item for item in children if item]
        return "; ".join(dict.fromkeys(children)) or str(exc)
    return str(exc).strip() or exc.__class__.__name__


_IDEASHELL_DATE_PREFIX = re.compile(r"^\d{4}\s+")


def ideashell_note_title(record: RecordingRecord, max_length: int = 30) -> str:
    """Build a short, date-prefixed title that fits IdeaShell's schema."""
    metadata = record.metadata
    times = [
        (metadata.get("course_time") or {}).get("start")
        if isinstance(metadata.get("course_time"), dict) else None,
        metadata.get("recorded_at"), metadata.get("started_at"),
    ]
    date_prefix = ""
    for value in times:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            stamp = datetime.fromisoformat(value.strip().replace("/", "-"))
        except ValueError:
            continue
        date_prefix = stamp.strftime("%m%d")
        break
    base = _IDEASHELL_DATE_PREFIX.sub("", str(metadata.get("title") or "未命名录音").strip()).strip()
    title = f"{date_prefix} {base}" if date_prefix else base
    limit = max(1, int(max_length or 30))
    return title[:limit].strip() or "课堂"


async def ideashell_recording_audio_parts(record: RecordingRecord) -> list[dict[str, Any]]:
    """Prepare attachments in a temporary directory, returning only bytes."""
    from qwen_asr_app.media_retention import file_worker
    try:
        source = Path(await file_worker(record.ensure_aac))
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"这条录音没有可用的音频文件：{exc}") from exc
    title = safe_component(str(record.metadata.get("title") or "课堂录音"), "课堂录音")

    def prepare():
        try:
            item = audio_file_metadata(source, filename=f"{title}.aac")
            return [{**item, "part_index": 1, "part_count": 1}]
        except ValueError as exc:
            if "超过闪念贝壳单个录音附件" not in str(exc):
                raise
        source_bytes = source.stat().st_size
        with tempfile.TemporaryDirectory(prefix="qwenasr-ideashell-audio-") as raw:
            destinations = [Path(raw) / f"part-{index}.aac" for index in (1, 2)]
            def read_parts(**encoding):
                return [{**audio_file_metadata(path, filename=f"{title}（第{index}段）.aac"),
                         "source_bytes": source_bytes, "part_index": index, "part_count": 2, **encoding}
                        for index, path in enumerate(destinations, 1)]
            try:
                split_audio_to_aac_lossless(source, destinations)
                return read_parts(compressed=False, encoding="copy")
            except (ValueError, OSError):
                pass
            last_error = None
            for bitrate in (32_000, 24_000, 16_000, 12_000):
                try:
                    split_audio_to_aac(source, destinations, bit_rate=bitrate)
                    return read_parts(compressed=True, bit_rate=bitrate, encoding="aac_reencode")
                except (ValueError, OSError) as exc:
                    last_error = exc
            raise ValueError(f"录音拆分后仍无法满足闪念贝壳附件限制：{last_error}")

    return await file_worker(prepare)


def ideashell_audio_preview(metadata: dict[str, Any]) -> dict[str, Any]:
    """Strip raw bytes before returning attachment information to the browser."""
    keys = ("name", "bytes", "sha256", "compressed", "source_bytes", "bit_rate")
    return {key: metadata[key] for key in keys if key in metadata}


def ideashell_folders(payload: Any) -> list[dict[str, str]]:
    folders: list[dict[str, str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            folder_id = nested_string(value, {"folder_id", "folderId", "id"})
            name = nested_string(value, {"name", "title", "folder_name", "folderName"})
            if re.fullmatch(r"[a-f0-9]{32}", folder_id) and name:
                item = {"id": folder_id, "name": name}
                if item not in folders:
                    folders.append(item)
            for child in value.values():
                visit(child)
            text_value = value.get("text")
            if isinstance(text_value, str):
                for match in re.finditer(
                    r"-\s*(.+?)\s*\[folder_id:\s*([a-f0-9]{32})\]",
                    text_value,
                    re.I,
                ):
                    folders.append(
                        {"id": match.group(2), "name": match.group(1).strip()}
                    )
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return folders


async def ensure_ideashell_course_folder(connection: Any, category: str) -> str:
    """Read remote folders first, then reuse or create the matching course folder."""
    listing = await connection.call_tool("folder_list", {})
    normalized = str(category or "未分类").strip() or "未分类"
    def folder_name(value: str) -> str:
        value = re.sub(r"^\s*[^\w\u4e00-\u9fff]+\s*", "", value, flags=re.UNICODE)
        return re.split(r"\s+#", value, maxsplit=1)[0].strip()

    for folder in ideashell_folders(listing):
        candidate = folder_name(folder["name"])
        if candidate == normalized or candidate.startswith(f"{normalized} "):
            return folder["id"]
    created = await connection.call_tool("folder_create", {"name": normalized})
    return ideashell_folder_id(created)


@app.post("/api/ideashell/connect")
async def ideashell_connect(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        connection = ideashell_manager.connection(str(payload.get("server_url") or ""))
        return await connection.start_connect(str(payload.get("callback_url") or ""))
    except (ValueError, ImportError, ModuleNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ideashell/status")
async def ideashell_status(request: Request, server_url: str) -> dict[str, Any]:
    try:
        connection = ideashell_manager.connection(server_url)
        status = connection.status()
        if status["status"] == "saved":
            # Resume saved authorization after a backend restart without a login popup.
            return await connection.start_connect(str(request.base_url).rstrip("/") + "/api/ideashell/oauth/callback", wait_seconds=0)
        return status
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ideashell/oauth/callback", response_class=HTMLResponse)
async def ideashell_oauth_callback(
    code: str = "", state: str | None = None, iss: str | None = None, error: str = ""
) -> HTMLResponse:
    if error:
        return HTMLResponse(
            f"<h2>IdeaShell 授权未完成</h2><p>{html.escape(error)}</p>", status_code=400
        )
    if not code:
        return HTMLResponse("<h2>IdeaShell 授权回调缺少 code</h2>", status_code=400)
    try:
        await ideashell_manager.deliver_callback(code, state, iss)
    except ValueError as exc:
        return HTMLResponse(
            f"<h2>IdeaShell 授权未完成</h2><p>{html.escape(str(exc))}</p>",
            status_code=400,
        )
    return HTMLResponse(
        "<meta charset='utf-8'><title>IdeaShell 已授权</title>"
        "<style>body{font-family:-apple-system;padding:40px;color:#17211d}</style>"
        "<h2>IdeaShell 授权已提交</h2><p>你可以关闭此窗口并返回 QwenASR。</p>"
        "<script>setTimeout(()=>window.close(),1200)</script>"
    )


@app.post("/api/recordings/{session_id}/ideashell/preview")
async def preview_ideashell_push(session_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    try:
        mode = str(payload.get("content_mode") or "combined")
        try:
            audio_parts = await ideashell_recording_audio_parts(record)
            audio_error = ""
        except (ValueError, OSError) as exc:
            audio_parts = []
            audio_error = str(exc)
        archive = compose_push(record, mode)
        archive.update(note_count=1, split=len(audio_parts) > 1,
                       audio_parts=[ideashell_audio_preview(audio) for audio in audio_parts])
        if audio_parts:
            archive["audio"] = ideashell_audio_preview(audio_parts[0])
        if audio_error:
            archive["audio_error"] = audio_error
        archive["title"] = ideashell_note_title(record)
        archive['sync'] = record.metadata.get('ideashell_sync') or {}
        return archive
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/recordings/{session_id}/ideashell/push")
@recording_operation
async def push_recording_summary_to_ideashell(
    session_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    summary = record.metadata.get("ai_note_summary")
    try:
        mode = str(payload.get("content_mode") or "combined")
        try:
            audio_parts = await ideashell_recording_audio_parts(record)
            audio_error = ""
        except (ValueError, OSError) as exc:
            audio_parts = []
            audio_error = str(exc)
        archive = compose_push(record, mode)
        legacy_preview = payload.get("split_parts") or []
        if len(audio_parts) == 2 and legacy_preview:
            midpoint = max(1.0, float(record.duration) / 2.0)
            archives = [
                compose_push_part(record, mode, 0, midpoint, 1),
                compose_push_part(record, mode, midpoint, float(record.duration), 2),
            ]
            preview_parts = legacy_preview
            if len(preview_parts) != 2 or any(
                str(preview_parts[index].get("digest") or "") != archives[index]["digest"]
                or str(preview_parts[index].get("audio_sha256") or "") != audio_parts[index]["sha256"]
                for index in range(2)
            ):
                raise ValueError("内容或录音已更新，请重新预览后推送")
        else:
            if payload.get("preview_digest") and payload["preview_digest"] != archive["digest"]:
                raise ValueError("内容已更新，请重新预览后推送")
            preview_hashes = payload.get("audio_sha256s")
            if preview_hashes is not None and preview_hashes != [audio["sha256"] for audio in audio_parts]:
                raise ValueError("录音文件已更新，请重新预览后推送")
            if len(audio_parts) > 1 and preview_hashes is None:
                raise ValueError("请先预览两个录音附件后再推送")
            preview_audio_sha256 = str(payload.get("audio_sha256") or "").strip()
            if preview_audio_sha256 and audio_parts and preview_audio_sha256 != audio_parts[0]["sha256"]:
                raise ValueError("录音文件已更新，请重新预览后推送")
        sync = record.metadata.get("ideashell_sync")
        sync = dict(sync) if isinstance(sync, dict) else {}
        previous_ids = [str(value) for value in (sync.get("note_ids") or []) if value]
        if not previous_ids and sync.get("note_id"):
            previous_ids = [str(sync["note_id"])]
        create_new = payload.get("create_new") is True
        operation = sync.get('create_operation') or {}
        if operation.get('status') in {'sending', 'unknown'} and not create_new:
            operation['status'] = 'unknown'
            sync.update(status='unknown', create_operation=operation)
            record.metadata['ideashell_sync'] = sync
            record.save_metadata()
            raise HTTPException(409, '上次新建结果未知，普通重试不会再次新建。请在 IdeaShell 查找后填写笔记 ID 核对关联；确认需要另一篇时，单独选择“明确新建另一篇”。')
        server_url = sync.get("server_url") if previous_ids else payload.get("server_url")
        connection = ideashell_manager.connection(
            str(server_url or payload.get("server_url") or "")
        )
        folder_id = await ensure_ideashell_course_folder(
            connection, str(record.metadata.get("category") or "未分类")
        )
        schemas = await connection.list_tools()
        note_ids: list[str] = []
        # Split audio is an attachment concern: keep one archive note and add
        # both halves to that same note instead of creating two notes.
        note_id = previous_ids[0] if previous_ids and not create_new else ""
        if note_id and "folder_notes" in schemas:
            visible = await connection.call_tool("folder_notes", {"folder_id": folder_id})
            # note_detail/note_update can still access trashed notes. A
            # successful update alone therefore does not prove a usable sync.
            if note_id not in json.dumps(visible, ensure_ascii=False):
                raise ValueError("已关联的笔记不在课程文件夹中，可能已删除或移走。请在推送预览中选择“新建一篇笔记”后重试")
        excerpt = str(summary.get("abstract") or "").strip() if isinstance(summary, dict) else ""
        excerpt = excerpt[:100] or record._compact_summary(archive["content"], 180)
        schema = schemas.get("note_update" if note_id else "note_create", {})
        max_title = schema.get("properties", {}).get("title", {}).get("maxLength", 30)
        title = ideashell_note_title(record, int(max_title or 30))
        arguments = {"title": title, "content": archive["content"], "summary": excerpt}
        if audio_parts:
            arguments.update(audio_attachment_arguments_many(schema, audio_parts))
        if note_id:
            arguments["note_id"] = note_id
            result = await connection.call_tool("note_update", arguments)
        else:
            arguments.update({"tag": "QwenASR", "folder_id": folder_id})
            if operation:
                sync.setdefault('create_history', []).append(dict(operation))
            operation = {'version': 1, 'id': uuid.uuid4().hex, 'status': 'sending',
                         'server_url': connection.server_url, 'folder_id': folder_id,
                         'content_digest': archive['digest'],
                         'reconcile_digest': hashlib.sha256(archive['content'].strip().encode()).hexdigest(),
                         'audio_sha256s': [audio['sha256'] for audio in audio_parts],
                         'title': title, 'started_at': local_now()}
            sync.update(status='sending', create_operation=operation)
            record.metadata['ideashell_sync'] = sync
            # Durable intent must precede the possibly accepted remote request.
            record.save_metadata()
            try:
                result = await connection.call_tool("note_create", arguments)
                note_id = ideashell_note_id(result)
                if not note_id:
                    raise RuntimeError('IdeaShell 未返回笔记编号')
            except BaseException:
                operation.update(status='unknown', updated_at=local_now())
                sync['status'] = 'unknown'
                record.save_metadata()
                raise
            operation.update(status='confirmed', note_id=note_id, updated_at=local_now())
        note_ids.append(note_id)
        old_ids = list(dict.fromkeys([*(sync.get("legacy_note_ids") or []), *(previous_ids if create_new else previous_ids[1:])]))
        sync.update({"note_ids": note_ids, "note_id": note_ids[0], "status": "partial",
                     "server_url": connection.server_url, "legacy_note_ids": old_ids})
        record.metadata["ideashell_sync"] = sync
        record.save_metadata()
        # Persist the created/updated note before moving it. A failed move
        # must not lose the note id and cause a duplicate on the next push.
        await connection.call_tool("note_move", {"note_ids": [note_id], "folder_id": folder_id})
        sync.update(
            {
                "note_id": note_ids[0],
                "note_ids": note_ids,
                "server_url": connection.server_url,
                "last_pushed_at": local_now(),
                "status": "synced",
                "folder_id": folder_id,
                "folder_name": str(record.metadata.get("category") or "未分类"),
                "content_mode": mode,
                "content_digest": archive["digest"],
                "audio_pushed_at": local_now(),
                "split_parts": max(1, len(audio_parts)),
            }
        )
        if audio_parts:
            sync.update({
                "audio_filename": [item["name"] for item in audio_parts] if len(audio_parts) > 1 else audio_parts[0]["name"],
                "audio_sha256": [item["sha256"] for item in audio_parts] if len(audio_parts) > 1 else audio_parts[0]["sha256"],
                "audio_bytes": [item["bytes"] for item in audio_parts] if len(audio_parts) > 1 else audio_parts[0]["bytes"],
            })
        if audio_error:
            sync["audio_error"] = audio_error
        else:
            sync.pop("audio_error", None)
        record.metadata["ideashell_sync"] = sync
        record.save_metadata()
        return {
            "synced": True,
            "direction": "push",
            "note_id": note_ids[0],
            "note_ids": note_ids,
            "parts": max(1, len(audio_parts)),
            "note_count": 1,
            "audio_attached": bool(audio_parts),
            "audio": [ideashell_audio_preview(item) for item in audio_parts] if len(audio_parts) > 1 else (ideashell_audio_preview(audio_parts[0]) if audio_parts else None),
            "audio_error": audio_error,
            "session": record.public(include_segments=False),
        }
    except HTTPException:
        raise
    except (ValueError, RuntimeError, ImportError, ModuleNotFoundError, OSError) as exc:
        print(f"IdeaShell 推送失败 recording={session_id}: {exc}", flush=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        detail = ideashell_error_text(exc)
        print(f"IdeaShell 推送异常 recording={session_id}: {detail}", flush=True)
        raise HTTPException(status_code=502, detail=f"IdeaShell 推送失败：{detail}") from exc


@app.post('/api/recordings/{session_id}/ideashell/reconcile')
@recording_operation
async def reconcile_ideashell_create(session_id: str, payload: dict[str, Any] = Body(...)):
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(404, '没有找到这条录音')
    sync = copy.deepcopy(record.metadata.get('ideashell_sync') or {})
    operation = sync.get('create_operation') or {}
    if operation.get('status') not in {'sending', 'unknown'}:
        raise HTTPException(409, '没有待核对的新建操作')
    note_id = str(payload.get('note_id') or '').strip()
    if not re.fullmatch(r'[a-f0-9]{32}', note_id):
        raise HTTPException(400, '请填写 IdeaShell 中实际存在的笔记 ID')
    try:
        connection = ideashell_manager.connection(operation['server_url'])
        result = await connection.call_tool('note_detail', {'note_id': note_id, 'scope': 'full'})
        content = ideashell_note_content(result)
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(502, '核对未完成，保留未知状态：' + str(exc)) from exc
    if not content or hashlib.sha256(content.strip().encode()).hexdigest() != operation['reconcile_digest']:
        raise HTTPException(409, '远端正文与发送时摘要不一致，保留未知状态；请人工核对，不会覆盖远端内容')
    # The protocol has no reliable idempotent create/search guarantee. Only an
    # explicitly selected ID with matching original content establishes a link.
    operation.update(status='confirmed', note_id=note_id, reconciled_at=local_now())
    previous_ids = sync.get('note_ids') or ([sync['note_id']] if sync.get('note_id') else [])
    sync['legacy_note_ids'] = list(dict.fromkeys([*(sync.get('legacy_note_ids') or []),
                                                 *(value for value in previous_ids if value != note_id)]))
    sync.update(note_id=note_id, note_ids=[note_id], server_url=operation['server_url'],
                folder_id=operation['folder_id'], status='partial', create_operation=operation)
    record.metadata['ideashell_sync'] = sync
    record.save_metadata()
    return {'linked': True, 'session': record.public(include_segments=False)}


@app.post("/api/recordings/{session_id}/ideashell/pull")
@recording_operation
async def pull_recording_summary_from_ideashell(
    session_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    sync = record.metadata.get("ideashell_sync")
    sync = dict(sync) if isinstance(sync, dict) else {}
    note_id = str(sync.get("note_id") or "")
    if not note_id:
        raise HTTPException(status_code=400, detail="这条笔记尚未推送到 IdeaShell，无法确定远端条目")
    try:
        connection = ideashell_manager.connection(
            str(sync.get("server_url") or payload.get("server_url") or "")
        )
        result = await connection.call_tool("note_detail", {"note_id": note_id, "scope": "full"})
        markdown = ideashell_note_content(result)
        summary = record.update_note_summary(summary_from_archive(markdown, sync), source="ideashell_pull")
        sync.update(
            {
                "note_id": note_id,
                "server_url": connection.server_url,
                "last_pulled_at": local_now(),
                "status": "synced",
            }
        )
        record.metadata["ideashell_sync"] = sync
        record.save_all()
        return {"synced": True, "direction": "pull", "note_id": note_id, "summary": summary, "session": record.public(include_segments=False)}
    except (ValueError, RuntimeError, ImportError, ModuleNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"IdeaShell 拉取失败：{exc}") from exc


@app.delete("/api/recordings/{session_id}")
@recording_operation
async def delete_recording(session_id: str) -> dict[str, Any]:
    try:
        trashed_path = recording_store.delete(session_id)
        return {
            "deleted": True,
            "recoverable": True,
            "message": "录音已移到系统废纸篓",
            "trashed_path": str(trashed_path),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _quiesce_discarded_recording(record, session):
    """Drain target writers; never cancel an AAC or accepted inference thread."""
    from qwen_asr_app.recording_discard import finish_owned
    if session is not None:
        session.reset_audio()  # All accepted microphone samples are already in WAV.
        if session.remote_lease_task:
            lease = session.remote_lease_task
            session.remote_lease_task = None
            lease.cancel()
            await asyncio.gather(lease, return_exceptions=True)
    finalizer = finalization_tasks.get(record.id)
    if finalizer is not None and not finalizer.done():
        await finish_owned(finalizer)
    if session is not None:
        await session.drain(include_ai=False)
        await session.suspend_ai_for_archive()
        await session.release_remote_compute()
    # Manual and summary jobs can own exported files or a renderer thread.
    # They finish before the directory is moved, without touching other jobs.
    pending = [background_summary_tasks.get(record.id)]
    pending.extend(task for key, task in background_ai_tasks.items() if key.endswith(':' + record.id))
    summary = summary_jobs.get(record.id)
    if summary:
        pending.append(summary.get('task'))
    for task in set(pending):
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await finish_owned(task)
            except asyncio.CancelledError:
                if not task.done():raise
            except Exception:
                pass  # Its finished failure is retained in the recording.
    from qwen_asr_app.classroom_intelligence.store import read_notes_doc
    intelligence = intelligence_router.intelligence_service
    for note in read_notes_doc(record).get('notes', []):
        await intelligence.cancel_task(note['id'])
    # Existing HTTP classroom questions/recording operations expose their
    # ownership until their final save; the middleware fences new requests.
    while record.id in record_operations or record.id in classroom_chat_running:
        await asyncio.sleep(.05)


from qwen_asr_app.recording_discard import RecordingDiscard
recording_discard = RecordingDiscard(recording_store, live_sessions, _quiesce_discarded_recording)


@app.post('/api/recordings/{session_id}/discard')
async def discard_recording(session_id: str, payload: dict[str, Any] = Body(...)):
    if payload.get('confirmed') is not True or payload.get('expected_session_id') != session_id:
        raise HTTPException(400, '请再次确认要丢弃的具体录音')
    from qwen_asr_app.live_ownership import control_lock
    async with control_lock():
        try:
            result = recording_discard.start(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
    return JSONResponse(result, status_code=202 if result['status'] == 'discarding' else 200)


@app.get('/api/recordings/{session_id}/discard')
async def discarded_recording_status(session_id: str):
    try:
        return recording_discard.snapshot(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/session")
async def current_session() -> dict[str, Any]:
    record = recording_store.active
    return {
        "active": bool(record and record.metadata.get("status") == "active"),
        "session": record.public() if record else None,
    }


@app.get("/api/recordings/{session_id}")
async def get_recording(session_id: str) -> dict[str, Any]:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    return {"session": record.public()}


@app.post("/api/recordings/{session_id}/segments/{segment_id}/retry-ai")
@recording_operation
async def retry_segment_ai(
    session_id: str,
    segment_id: int,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """Retry one failed sentence correction/translation without rebuilding the note."""
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    segment = next(
        (item for item in record.segments if int(item.get("id", -1)) == int(segment_id)),
        None,
    )
    if segment is None:
        raise HTTPException(status_code=404, detail="没有找到这段转写")
    raw_text = str(segment.get("raw") or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="这段没有可供 AI 处理的原始转写")

    key = (record.id, int(segment_id))
    lock = segment_retry_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if segment.get("ai_pending"):
            raise HTTPException(status_code=409, detail="这段正在处理中，请稍候")

        live_session = live_sessions.get(record.id)
        if record.metadata.get("status") == "active" and live_session is not None:
            config = json.loads(json.dumps(live_session.config))
        else:
            provided = payload.get("config") if isinstance(payload, dict) else None
            config = json.loads(json.dumps(provided)) if isinstance(provided, dict) else {}
            if not config:
                saved = record.metadata.get("batch_options")
                config = json.loads(json.dumps(saved)) if isinstance(saved, dict) else {}
            if record.metadata.get("kind") == "upload":
                config["_batch_segment_mode"] = True

        course = record.metadata.get("course_settings") or {}
        try:
            config = ai_preferences.task_config(
                config, course=course, operation='segment',
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        config.setdefault("terminology", course.get("terminology") or [])

        if not bool(config.get("ai_correction")) and str(config.get("translation", "off")) == "off":
            raise HTTPException(status_code=400, detail="请先开启 AI 修正或翻译")

        previous = [
            str(item.get("corrected") or item.get("raw") or "")
            for item in record.segments
            if int(item.get("id", 0)) < int(segment_id)
        ]
        ai_options = config.get("ai") if isinstance(config.get("ai"), dict) else {}
        try:
            history_chars = max(0, min(int(ai_options.get("history_chars", 2000)), 12_000))
        except (TypeError, ValueError):
            history_chars = 2000
        config["_transcript_context"] = "\n".join(previous)[-history_chars:] if history_chars else ""
        segment["ai_requested"] = {
            "correction": bool(config.get("ai_correction")),
            "translation": str(config.get("translation", "off")),
        }
        segment["ai_pending"] = True
        segment.pop("ai_error", None)
        record.upsert_segment(segment)
        try:
            enrichment = await polish_and_translate(
                raw_text,
                str(segment.get("language") or ""),
                config,
            )
            segment.update(enrichment)
            if not segment_ai_complete(segment, config):
                raise RuntimeError("AI 返回结果缺少所需的修正或翻译字段，原文已保留")
            segment["ai_pending"] = False
            segment.pop("ai_error", None)
            record.upsert_segment(segment)
            return {"success": True, "segment": segment}
        except Exception as exc:
            segment["ai_pending"] = False
            segment["ai_error"] = str(exc)
            segment["error_kind"] = str(getattr(exc, "kind", "upstream"))
            record.upsert_segment(segment)
            return {
                "success": False,
                "segment": segment,
                "error": str(exc),
                "error_kind": str(getattr(exc, "kind", "upstream")),
            }


@app.get("/api/recordings/{session_id}/download/{kind}")
@recording_operation
async def download_recording(session_id: str, kind: str) -> FileResponse:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这次录音")
    if session_id in running_batch_ids:
        raise HTTPException(409, "课次正在补齐或归档，请稍后再下载")
    if record.metadata.get("status") != "completed":
        raise HTTPException(status_code=409, detail="请先结束并保存录音，再下载文件")
    if kind == "bundle":
        export = asyncio.create_task(asyncio.to_thread(record.make_bundle))
        cancelled = False
        while True:
            try:
                path = await asyncio.shield(export)
                break
            except asyncio.CancelledError:
                if export.cancelled():
                    raise
                cancelled = True
        if cancelled:
            # Retain record ownership until the thread's writes finish, and
            # remove the temporary download if there is no response to own it.
            path.unlink(missing_ok=True)
            raise asyncio.CancelledError
        filename = f"{session_id}-录音与转写.zip"
        media_type = "application/zip"
    elif kind == "audio":
        path = record.ensure_aac()
        filename = f"{session_id}{path.suffix}"
        media_type = {
            ".aac": "audio/aac",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
        }.get(path.suffix.lower(), "application/octet-stream")
    elif kind == "ai-summary":
        from qwen_asr_app.note_pdf import reviewed_pdf
        try:
            path = await asyncio.to_thread(reviewed_pdf, record)
        except (ValueError, RuntimeError) as exc:
            if (record.metadata.get('ai_note_summary') or {}).get('summary'):
                async def prepare_requested_pdf():
                    await asyncio.sleep(0)  # release this download's record guard first
                    current=recording_store.find(session_id)
                    if current is not None:
                        try:start_summary_job(current,{'_pdf_only':True})
                        except (HTTPException,ValueError):pass
                asyncio.create_task(prepare_requested_pdf())
            raise HTTPException(409, str(exc)+'；已安排后台导出与审查，请在任务中心查看') from exc
        filename = f"{safe_component(str(record.metadata.get('title') or session_id), session_id)}-课后复习笔记.pdf"
        media_type = "application/pdf"
    elif kind == "transcript":
        record.save_transcripts()
        path = record.transcript_path
        filename = f"{session_id}-转写.txt"
        media_type = "text/plain; charset=utf-8"
    elif kind == "raw-transcript":
        record.save_transcripts()
        path = record.raw_transcript_path
        filename = f"{safe_component(str(record.metadata.get('title') or session_id), session_id)}-原始转写.txt"
        media_type = "text/plain; charset=utf-8"
    elif kind == "corrected-transcript":
        record.save_transcripts()
        path = record.corrected_transcript_path
        filename = f"{safe_component(str(record.metadata.get('title') or session_id), session_id)}-AI修正后文本.txt"
        media_type = "text/plain; charset=utf-8"
    elif kind == "json":
        path = record.transcript_json_path
        filename = f"{session_id}-转写.json"
        media_type = "application/json"
    else:
        raise HTTPException(status_code=404, detail="不支持的下载类型")
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件尚未生成")
    if kind == "bundle":
        return EphemeralFileResponse(
            path,
            filename=filename,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(path, filename=filename, media_type=media_type, headers={'Cache-Control':'no-store'})


def stream_event(event_type: str, **payload: Any) -> bytes:
    return (json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n").encode("utf-8")


running_batch_ids: set[str] = set()

from qwen_asr_app.course_stream import CourseStreamManager, course_stream_router
course_stream_manager = CourseStreamManager(
    model_manager, recording_store, app_settings, running_batch_ids,
    is_record_busy=recording_is_busy,
)
app.include_router(course_stream_router(course_stream_manager))


def redacted_batch_config(config: dict[str, Any]) -> dict[str, Any]:
    saved = json.loads(json.dumps(config))
    ai = saved.get("ai") if isinstance(saved.get("ai"), dict) else {}
    ai.pop("api_key", None)
    for provider_name in ("review_provider", "summary_provider", "global_provider", "correction_provider"):
        provider = ai.get(provider_name)
        if isinstance(provider, dict):
            provider.pop("api_key", None)
    saved["ai"] = ai
    saved.pop("type", None)
    return saved

async def batch_transcription_stream(
    temp_path: Path | None,
    original_filename: str,
    config: dict[str, Any],
    existing_record: RecordingRecord | None = None,
    preserve_source: bool = False,
):
    from qwen_asr_app.remote_operations import saved_config
    config = dict(config)
    if 'inference_settings' not in config:
        # Older queued/uploaded work has no provider snapshot. Treat it as a
        # legacy local operation; reading today's global remote preference
        # could silently move an old task to Windows during recovery.
        config['inference_settings'] = (
            existing_record.metadata.get('batch_options', {}).get('inference_settings')
            or saved_config()
            if existing_record
            else saved_config()
        )
    remote_cfg = config['inference_settings']
    record: RecordingRecord | None = existing_record
    pipeline = None
    warmup = None
    inference_task = None
    remote_recording_task = None
    asr_owned = True
    model_manager.active_jobs += 1
    model_manager.cancel_unload()
    started_processing = time.monotonic()
    try:
        source_path = temp_path
        canonical = Path(config['_remote_operation_dir'])/'audio.wav' if config.get('_remote_operation_dir') else None
        if canonical is not None and canonical.is_file():
            temp_path = canonical
            source_path = canonical
        if record is not None:
            # Durable upload source is retained until completion. Reuse it for
            # the same remote digest; AAC archival encoding is lossy.
            source_path = temp_path if temp_path is not None and temp_path.exists() else record.audio_path
            record.metadata["status"] = "processing"
            record.metadata.pop("error", None)
            record.metadata["updated_at"] = local_now()
            record.save_all()
        if source_path is None or not source_path.exists():
            raise ValueError("找不到可恢复的 AAC 录音文件")

        yield stream_event(
            "progress",
            progress=3,
            stage="正在解码录音" if existing_record is None else "正在恢复并解码 AAC",
            filename=original_filename,
        )
        audio = await asyncio.to_thread(decode_audio_file, source_path)
        duration = len(audio) / SAMPLE_RATE
        if duration < 0.2:
            raise ValueError("录音时间太短")
        if duration > 2 * 60 * 60:
            raise ValueError("单个录音最长支持 2 小时")

        # Validate and decode first. Invalid uploads must not wake the 4 GB
        # ASR worker just to fail after the file check.
        if remote_cfg['asr_provider'] == 'local':
            warmup = asyncio.create_task(model_manager.load(force_local=True))

        if record is None:
            title = str(config.get("title") or Path(original_filename).stem or "上传录音").strip()
            category = str(config.get("category") or "未分类")
            yield stream_event("progress", progress=7, stage="正在归档为 AAC", duration=round(duration, 1))
            creation = asyncio.create_task(asyncio.to_thread(
                recording_store.create_upload,
                source_path,
                original_filename,
                duration,
                title,
                category,
                session_id=config.get("_upload_recording_id"),
                course_time=config.get('course_time') if original_filename.startswith('smartclass_') else None,
            ))
            cancelled = False
            while True:
                try:
                    record = await asyncio.shield(creation)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                yield stream_event("accepted", session=record.public(include_segments=False))
                raise asyncio.CancelledError
        running_batch_ids.add(record.id)
        yield stream_event("accepted", session=record.public(include_segments=False))
        ai_options = config.get("ai") if isinstance(config.get("ai"), dict) else {}
        record.metadata["course_settings"] = {
            "asr_prompt": str(config.get("context") or ""),
            "terminology": normalize_terminology(config.get("terminology")),
            "asr_params": normalize_asr_params(
                asr_params_with_terminology(config.get("asr_params"), config.get("terminology")),
                DEFAULT_ASR_PARAMS["max_tokens"],
            ),
            "live_params": normalize_live_params(
                {key: config.get(key, value) for key, value in DEFAULT_LIVE_PARAMS.items()}
            ),
            "ai_system_prompt": str(ai_options.get("system_prompt") or DEFAULT_AI_SYSTEM_PROMPT),
        }
        record.metadata['inference_settings'] = remote_cfg
        record.metadata.setdefault('inference_attempts', []).append({'config_snapshot': remote_cfg, 'started_at': local_now()})
        record.metadata["batch_options"] = redacted_batch_config(config)
        record.metadata.setdefault("ai_groups", [])
        corrupt_segments = [
            segment for segment in record.segments if has_repetitive_tail(str(segment.get("raw") or ""))
        ] if remote_cfg['asr_provider'] == 'local' else []
        if corrupt_segments:
            rewind_seconds = min(float(segment.get("start", 0)) for segment in corrupt_segments)
            removed_ids = {
                int(segment.get("id", 0))
                for segment in record.segments
                if float(segment.get("start", 0)) >= rewind_seconds
            }
            record.segments = [
                segment
                for segment in record.segments
                if float(segment.get("start", 0)) < rewind_seconds
            ]
            record.metadata["ai_groups"] = [
                group
                for group in record.metadata.get("ai_groups", [])
                if isinstance(group, dict)
                and not removed_ids.intersection(
                    int(value) for value in group.get("segment_ids", [])
                )
            ]
            record.metadata["next_segment_id"] = max(
                [int(segment.get("id", 0)) for segment in record.segments] + [0]
            ) + 1
            record.save_all()
            yield stream_event(
                "retry",
                progress=10 + round(58 * rewind_seconds / max(duration, 0.01)),
                stage=f"发现历史重复段，正从 {format_clock(rewind_seconds)} 回退重转",
                start=rewind_seconds,
                end=duration,
            )
        completed_end = max(
            max((float(segment.get("end", 0)) for segment in record.segments), default=0.0),
            0.0 if corrupt_segments else float(record.metadata.get("batch_progress", {}).get("completed_seconds", 0)),
        )
        start_sample = min(len(audio), max(0, int(round(completed_end * SAMPLE_RATE))))
        record.metadata["batch_progress"] = {
            "stage": "asr",
            "audio_duration": round(duration, 2),
            "completed_seconds": round(completed_end, 2),
            "percent": round(68 * start_sample / max(1, len(audio)), 1),
            "message": "正在从上次成功位置继续" if start_sample else "正在分段转写",
        }
        record.save_all()
        yield stream_event(
            "session",
            progress=10 + round(58 * start_sample / max(1, len(audio))),
            stage="从上次成功位置继续" if start_sample else "开始智能分段转写",
            session=record.public(include_segments=False),
        )

        needs_ai = bool(config.get("ai_correction")) or config.get("translation", "off") != "off"
        if needs_ai:
            pipeline = SegmentCorrectionPipeline(record, config)
            for saved_segment in list(record.segments):
                if not segment_ai_complete(saved_segment, config):
                    await pipeline.submit(saved_segment)

        # An offline recording is one remote job: load Qwen once, process every
        # remote segment, then reuse the existing storage/AI correction pipeline.
        from qwen_asr_app.inference_providers import inference_settings, RemoteASRProvider
        remote_segments = None
        remote_batch_fallback = None
        remote_batch_metadata = {}
        if remote_cfg["asr_provider"] != "local" and start_sample < len(audio):
            inference_task = asyncio.create_task(RemoteASRProvider().transcribe_segments(
                audio, config.get("language"),
                asr_terminology_context(str(config.get("context", "")), config.get("terminology")),
                int((config.get('asr_params') or {}).get('max_tokens', 2048)), remote_cfg, operation_dir=config.get('_remote_operation_dir'),
                operation_key=config.get('_remote_operation_key'), asr_params=config.get('asr_params')))
            remote_recording_task = inference_task
            try:
                while not inference_task.done():
                    await asyncio.wait({inference_task}, timeout=15)
                    if not inference_task.done():
                        yield stream_event("progress", progress=record.metadata["batch_progress"]["percent"],
                                           stage="Windows 正在处理整段录音 · 冷启动及排队可耗时较长")
                value, remote_batch_metadata = inference_task.result()
                remote_segments = value["segments"]
                if not remote_segments:
                    raise RuntimeError("远程没有返回音频分段")
                record.metadata["inference"] = remote_batch_metadata
                record.save_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if remote_cfg["asr_provider"] == "remote_only" or not getattr(exc, 'proven_not_accepted', False):
                    raise
                from qwen_asr_app.remote_operations import mark_local_fallback
                mark_local_fallback(Path(config['_remote_operation_dir'])/'receipt.json')
                remote_segments = None
                remote_batch_fallback = type(exc).__name__
                record.metadata["inference"] = {"provider":"local", "fallback":True,
                    "fallback_reason":remote_batch_fallback, 'config_snapshot': remote_cfg,
                    'metrics': {'model': str(getattr(model_manager, 'model_path', Path('未知模型')).name),
                                'dtype': 'bfloat16', 'device': 'Apple Silicon MLX',
                                'gpu_release_state': 'unknown'}}
                record.save_all()
        if remote_cfg['asr_provider'] == 'local':
            record.metadata['inference'] = {'provider': 'local', 'fallback': False, 'config_snapshot': remote_cfg,
                'metrics': {'model': str(getattr(model_manager, 'model_path', Path('未知模型')).name), 'dtype': 'bfloat16', 'device': 'Apple Silicon MLX', 'gpu_release_state': 'unknown'}}
        record.metadata['inference_attempts'][-1]['actual'] = record.metadata.get('inference', {})
        record.save_metadata()
        remote_by_range = {}
        if remote_segments is not None:
            ranges = deque()
            for segment in remote_segments:
                start = round(float(segment['start'])*SAMPLE_RATE)
                end = min(len(audio), round(float(segment['end'])*SAMPLE_RATE))
                if end <= start_sample:
                    continue
                start = max(start_sample, start)
                if not start_sample <= start < end <= len(audio):
                    raise RuntimeError("远程分段时间范围无效")
                ranges.append((start,end))
                remote_by_range[(start,end)] = segment
        else:
            ranges = deque(vad_split_ranges(audio, start_sample=start_sample))
        estimated_total = len(record.segments) + len(ranges)
        while ranges:
            segment_start_sample, segment_end_sample = ranges.popleft()
            if remote_segments is None and getattr(model_manager, "realtime_pending", False) and segment_end_sample - segment_start_sample > 25 * SAMPLE_RATE:
                ranges.appendleft((segment_start_sample + 25 * SAMPLE_RATE, segment_end_sample))
                segment_end_sample = segment_start_sample + 25 * SAMPLE_RATE
            block = audio[segment_start_sample:segment_end_sample]
            block_duration = len(block) / SAMPLE_RATE
            max_tokens = min(1_536, max(256, int(block_duration * 10) + 128))
            metrics = {}
            try:
                if remote_segments is not None:
                    segment = remote_by_range[(segment_start_sample,segment_end_sample)]
                    async def prepared_result(segment=segment):
                        return segment.get("merged_text", segment["text"]), segment.get("language", ""), 0.0
                    metrics.update(remote_batch_metadata)
                    inference_task = asyncio.create_task(prepared_result())
                else:
                    transcribe = model_manager._transcribe_local if remote_batch_fallback else model_manager.transcribe
                    inference_kwargs = {} if remote_batch_fallback else {'inference_config': remote_cfg}
                    inference_task = asyncio.create_task(transcribe(
                        block,
                        config.get("language"),
                        asr_terminology_context(
                            str(config.get("context", "")), config.get("terminology")
                        ),
                        max_tokens=max_tokens,
                        asr_params=asr_params_with_terminology(
                            config.get("asr_params"), config.get("terminology")
                        ),
                        timeout_seconds=180, metrics=metrics,
                        **inference_kwargs,
                    ))
                    if remote_batch_fallback:
                        metrics.update(provider="local", fallback=True, fallback_reason=remote_batch_fallback)
                while not inference_task.done():
                    event_task = asyncio.create_task(pipeline.events.get()) if pipeline else None
                    waiting = {inference_task} | ({event_task} if event_task else set())
                    try:
                        done, _ = await asyncio.wait(waiting, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                        if event_task and event_task in done:
                            event = event_task.result()
                            yield stream_event(event.pop("type"), progress=record.metadata["batch_progress"]["percent"], **event)
                        elif not done:
                            yield stream_event("progress", progress=record.metadata["batch_progress"]["percent"],
                                               stage="本地模型正在转写 · AI 精修并行处理中" if pipeline else "本地模型正在转写")
                    finally:
                        if event_task and not event_task.done():
                            event_task.cancel()
                            await asyncio.gather(event_task, return_exceptions=True)
                raw, language, seconds = inference_task.result()
                if remote_segments is None and has_repetitive_tail(raw):
                    if block_duration > 35:
                        raise RuntimeError("检测到异常重复输出")
                    raw = trim_repetitive_tail(raw)
            except Exception as exc:
                if block_duration > 35 and ("超时" in str(exc) or "重复" in str(exc)):
                    first_half, second_half = bisect_audio_range(
                        audio, segment_start_sample, segment_end_sample
                    )
                    ranges.appendleft(second_half)
                    ranges.appendleft(first_half)
                    estimated_total += 1
                    yield stream_event(
                        "retry",
                        progress=10 + round(58 * segment_start_sample / max(1, len(audio))),
                        stage="片段耗时异常，已自动切成更短两段重试",
                        start=round(segment_start_sample / SAMPLE_RATE, 2),
                        end=round(segment_end_sample / SAMPLE_RATE, 2),
                    )
                    continue
                raise
            next_segment_id = max(
                [int(item.get("id", 0)) for item in record.segments] + [0]
            ) + 1
            if not raw:
                record.metadata["batch_progress"]["completed_seconds"] = round(
                    segment_end_sample / SAMPLE_RATE, 2
                )
                record.save_all()
                continue
            segment = {
                "id": next_segment_id,
                "raw": raw,
                "language": language,
                "latency": seconds, "metrics": metrics,
                "duration": round(block_duration, 1),
                "start": round(segment_start_sample / SAMPLE_RATE, 2),
                "end": round(segment_end_sample / SAMPLE_RATE, 2),
                "reason": "upload_vad_block",
                "ai_pending": bool(config.get("ai_correction"))
                or config.get("translation", "off") != "off",
            }
            if remote_segments is not None:
                original = remote_by_range[(segment_start_sample, segment_end_sample)]
                segment['remote_raw_segment'] = copy.deepcopy(original)
            if segment["ai_pending"]:
                segment["ai_requested"] = {
                    "correction": bool(config.get("ai_correction")),
                    "translation": str(config.get("translation", "off")),
                }
            record.metadata["next_segment_id"] = next_segment_id + 1
            completed_seconds = segment_end_sample / SAMPLE_RATE
            asr_percent = 10 + round(58 * segment_end_sample / max(1, len(audio)))
            record.metadata["batch_progress"] = {
                "stage": "asr",
                "audio_duration": round(duration, 2),
                "completed_seconds": round(completed_seconds, 2),
                "percent": asr_percent,
                "message": f"原始转写已完成 {format_clock(completed_seconds)} / {format_clock(duration)}",
            }
            record.upsert_segment(segment)
            yield stream_event(
                "raw",
                progress=asr_percent,
                stage=(
                    f"原始转写 {format_clock(completed_seconds)} / {format_clock(duration)}"
                    f" · 约 {len(record.segments)}/{estimated_total} 段"
                ),
                segment=segment,
            )
            if pipeline:
                await pipeline.submit(segment)
                while not pipeline.events.empty():
                    event = pipeline.events.get_nowait()
                    yield stream_event(event.pop("type"), progress=asr_percent, **event)

        # Only the decode/recognition phase needs a local model lifetime lease.
        # Cloud correction and global rewriting may take much longer than ASR.
        if inference_task and not inference_task.done():
            await asyncio.gather(inference_task, return_exceptions=True)
        if warmup:
            await asyncio.gather(warmup, return_exceptions=True)
        model_manager.active_jobs = max(0, model_manager.active_jobs - 1)
        asr_owned = False
        model_manager.schedule_unload()

        if pipeline:
            finish_task = asyncio.create_task(pipeline.finish())
            try:
                while not finish_task.done():
                    try:
                        event = await asyncio.wait_for(pipeline.events.get(), timeout=1)
                        yield stream_event(event.pop("type"), progress=68, **event)
                    except asyncio.TimeoutError:
                        yield stream_event("progress", progress=68, stage="原始转写完成 · 等待剩余 AI 精修")
                await finish_task
                while not pipeline.events.empty():
                    event = pipeline.events.get_nowait()
                    yield stream_event(event.pop("type"), progress=68, **event)
            finally:
                if not finish_task.done():
                    finish_task.cancel()
                    await asyncio.gather(finish_task, return_exceptions=True)

        if not record.segments:
            raise ValueError("没有从录音中识别到文字")

        if needs_ai:
            record.metadata["batch_progress"] = {
                "stage": "global_ai",
                "audio_duration": round(duration, 2),
                "completed_seconds": round(duration, 2),
                "percent": 70,
                "message": "逐段修正完成，正在进行全局改写",
            }
            record.save_all()
            yield stream_event("progress", progress=70, stage="逐段修正完成，AI 正在全局改写")
            async for ai_event in global_rewrite_events(record, config):
                if ai_event["type"] == "ai_global_part":
                    index = int(ai_event["index"])
                    count = max(1, int(ai_event["count"]))
                    progress = 70 + round(24 * int(ai_event.get("completed", index)) / count)
                    yield stream_event(
                        "ai_global_part",
                        progress=progress,
                        stage=f"全局改写 {index}/{count}",
                        part=ai_event["part"],
                        index=index,
                        count=count,
                    )
                else:
                    yield stream_event(
                        "ai_global_complete",
                        progress=95,
                        stage="AI 全局改写完成",
                        global_rewrite=ai_event["global_rewrite"],
                    )
        else:
            for segment in record.segments:
                segment["ai_pending"] = False
            record.save_all()

        record.metadata["processing_seconds"] = round(time.monotonic() - started_processing, 2)
        record.metadata["asr_seconds"] = round(
            float(remote_batch_metadata.get("metrics", {}).get("inference_seconds", 0))
            if remote_segments is not None else sum(float(s.get("latency", 0)) for s in record.segments), 2)
        record.metadata["ai_warnings"] = sum(bool(s.get("ai_error")) for s in record.segments) + sum(
            bool(part.get("ai_error")) for part in record.metadata.get("ai_global_rewrite", {}).get("parts", []))
        record.complete()
        yield stream_event(
            "complete",
            progress=100,
            stage="已保存 AAC 与转写",
            session=record.public(),
        )
    except asyncio.CancelledError:
        if record is not None and record.metadata.get("status") != "completed":
            record.metadata["status"] = "interrupted"
            record.metadata["error"] = "连接或服务关闭；可以从最后成功位置继续"
            progress = record.metadata.get("batch_progress")
            if isinstance(progress, dict):
                progress["stage"] = "interrupted"
                progress["message"] = record.metadata["error"]
            record.save_all()
        raise
    except Exception as exc:
        if record is not None and record.metadata.get("status") != "completed":
            record.metadata["status"] = "interrupted"
            record.metadata["error"] = str(exc)
            progress = record.metadata.get("batch_progress")
            if isinstance(progress, dict):
                progress["stage"] = "interrupted"
                progress["message"] = str(exc)
            record.save_all()
        yield stream_event("error", progress=record.metadata.get("batch_progress", {}).get("percent", 0) if record else 0, message=str(exc))
    finally:
        if pipeline:
            await pipeline.close()
        # A to_thread GPU call must finish before releasing its lifetime lease.
        if remote_recording_task and not remote_recording_task.done():
            # Propagate pause/cancel to the remote client so it cancels the Job
            # ID and waits for its streaming/polling thread to exit safely.
            remote_recording_task.cancel()
        if inference_task and not inference_task.done():
            await asyncio.gather(inference_task, return_exceptions=True)
        if warmup:
            await asyncio.gather(warmup, return_exceptions=True)
        if asr_owned:
            model_manager.active_jobs = max(0, model_manager.active_jobs - 1)
            model_manager.schedule_unload()
        if record:
            running_batch_ids.discard(record.id)
        if temp_path is not None and not preserve_source:
            temp_path.unlink(missing_ok=True)


from qwen_asr_app.upload_jobs import UploadJobs
upload_jobs = UploadJobs(batch_transcription_stream, recording_store.find, redacted_batch_config, _remember_ai_keys,
                         require_available=require_recording_available)


from qwen_asr_app.upload_routes import upload_router
app.include_router(upload_router(upload_jobs, recording_store, require_recording_available))


@app.websocket("/ws/transcribe")
async def transcribe_socket(websocket: WebSocket) -> None:
    if not websocket_origin_allowed(
        websocket.headers.get("origin"), str(websocket.url), os.getenv("QWEN_ASR_ALLOWED_ORIGINS", ""),
    ):
        await websocket.close(code=1008)
        return
    if service_draining or backup_manager.busy:
        await websocket.close(code=1013)
        return
    await websocket.accept()
    model_manager.client_connected()
    from qwen_asr_app.live_ownership import control_lock
    # Connecting is read-only. Ownership is acquired only by start/end, under
    # one lock, after re-reading the active recording (including preopened tabs).
    session = TranscriptionSession(websocket, model_manager, recording_store)
    await websocket.send_json({"type": "ready", "model_status": model_manager.status})
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if getattr(session, 'writer', None) is websocket:
                    await session.ingest(message["bytes"])
                continue
            text = message.get("text")
            if not text:
                continue
            payload = json.loads(text)
            kind = payload.get("type")
            if kind == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            async with control_lock():
                if getattr(session, 'discarding', False):
                    # The old socket cannot pause/end/resume a discarded
                    # session or bind its draining tasks to a new recording.
                    if kind not in {'config', 'start'}:
                        await websocket.send_json({'type': 'control_error', 'message': '这条录音已经停止并进入丢弃流程'})
                        continue
                    session = TranscriptionSession(websocket, model_manager, recording_store)
                active = recording_store.active
                canonical = live_sessions.get(active.id) if active else None
                if kind == "config":
                    # Observer settings stay on the observer until it acquires
                    # ownership; it cannot mutate the current writer's config.
                    if getattr(session, 'writer', None) not in (None, websocket):
                        session = TranscriptionSession(websocket, model_manager, recording_store)
                    try:
                        values = ai_preferences.task_config(payload, operation='live')
                        pending = session.update_config({**payload, **values})
                    except (OSError, ValueError) as exc:
                        await websocket.send_json({"type": "config_error", "message": str(exc)})
                        continue
                    await websocket.send_json({"type": "configured", "pending": pending,
                                               "warning": values.get('_ai_config_warning', '')})
                elif kind in {"start", "end", "pause", "stop"}:
                    if active and recording_discard.busy(active.id):
                        await websocket.send_json({'type': 'start_error' if kind == 'start' else 'control_error',
                                                   'message': '上一条录音正在安全丢弃，请完成后再开始'})
                        continue
                    writer = getattr(canonical, 'writer', None) if canonical else None
                    takeover = kind == 'start' and payload.get('takeover') is True
                    if writer is not None and writer is not websocket and not takeover:
                        await websocket.send_json({"type": "start_error" if kind == 'start' else "end_error" if kind == 'end' else "control_error",
                            "code": "writer_conflict", "message": "另一页面正在控制录音；如需接管，请明确选择接管录音"})
                        continue
                    if canonical and writer is not websocket:
                        if (active.metadata.get('finalization') or {}).get('status') == 'running':
                            await websocket.send_json({"type": "end_error", "message": "录音正在结束保存"})
                            continue
                        if writer is not None:
                            canonical.writer = None  # Fence old packets before any await.
                            await canonical.pause(send_status=False)
                            try:
                                await writer.send_json({"type": "ownership_lost", "message": "录音已由另一页面接管"})
                            except (RuntimeError, OSError, WebSocketDisconnect):
                                pass
                        canonical.update_config(session.config)
                        session = canonical
                    try:
                        session.ws = websocket
                        session.writer = websocket
                        session.recording = active
                        if kind == 'start':
                            session.update_config(ai_preferences.task_config(session.config, operation='live'))
                            try:
                                await session.start_or_resume()
                            finally:
                                if session.recording is not None:
                                    live_sessions[session.recording.id] = session
                        elif kind in {'pause', 'stop'}:
                            await session.pause()
                        else:
                            summary_payload = payload.get('summary_payload')
                            if not isinstance(summary_payload, dict):
                                summary_payload = {'config': session.config}
                            await begin_recording_end(session, summary_payload)
                    except (ValueError, OSError) as exc:
                        session.accepting_audio = False
                        if kind == 'start':
                            session.writer = None
                        await websocket.send_json({"type": "start_error" if kind == 'start' else "end_error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        async with control_lock():
            # An old connection's disconnect must never pause its successor.
            if getattr(session, 'writer', None) is websocket:
                session.writer = None
                if session.recording is not None and session.recording.id not in finalization_tasks:
                    await session.pause(send_status=False)
        model_manager.client_disconnected()


@app.get("/static/notes.html")
@app.get("/notes")
async def notes_page(request: Request) -> RedirectResponse:
    query = request.url.query
    return RedirectResponse("/course-files" + ("?" + query if query else ""), status_code=307)


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


from qwen_asr_app.knowledge.task_adapter import IntelligenceTasks
intelligence_tasks = IntelligenceTasks(knowledge_service, intelligence_router.intelligence_service, recording_store)
app.include_router(task_center_router(compact_ai_tasks, ai_task_action, course_file_api, pdf_jobs, upload_jobs, intelligence_tasks))


from qwen_asr_app.note_routes import note_router
from qwen_asr_app.note_library import NoteLibrary, note_library_router
app.include_router(note_router(recording_store, recording_operation))
review_library = NoteLibrary(app_settings, recording_store)
app.include_router(note_library_router(app_settings, recording_store, library=review_library))


def require_backup_idle():
    tasks = [*background_summary_tasks.values(), *background_ai_tasks.values(),
             *finalization_tasks.values(), *pdf_ai_background_tasks.values(),
             *recording_discard.tasks.values(),
             *course_file_api.tasks.values(), *upload_jobs.tasks.values(),
             *intelligence_router.intelligence_service._running_tasks.values(),
             *intelligence_router.intelligence_service._debounce_tasks.values(),
             smartclass_service.job, smartclass_service.catalog_job]
    if (service_draining or backup_manager.busy or service_mutations > 1
            or model_manager.active_jobs or model_manager.connected_clients
            or running_batch_ids or record_operations or classroom_chat_running
            or knowledge_service.job_manager._active_job_id or knowledge_service.job_manager._queue.qsize()
            or course_file_api.lock.locked() or pdf_ai_lock.locked()
            or any(task is not None and not task.done() for task in tasks)
            or any(record.metadata.get("status") in {"active", "processing"}
                   for record in recording_store.list_records())):
        raise HTTPException(409, "录音、课程或后台任务仍在处理，请完成或暂停后再导出日常备份")


app.include_router(backup_router(backup_manager,
                                lambda: backup_sources(app_settings, recording_store, review_library),
                                require_backup_idle))

# The course replay UI and API share this process and port.  Its internal
# routes remain unchanged, so the old course service can still be tested in
# isolation while the desktop launcher exposes it at http://127.0.0.1:8765/course/.
smartclass_service.app.state.recording_store = recording_store
app.mount("/course", smartclass_service.app)


@app.get("/workspace")
async def workspace_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "workspace.html", headers={"Cache-Control": "no-store"})


@app.get("/library")
async def library_page():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/tasks")
async def task_center_page():
    return FileResponse(ROOT / "static" / "tasks.html", headers={"Cache-Control": "no-store"})


@app.get("/course-files")
async def course_files_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "course-files.html", headers={"Cache-Control": "no-store"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        ROOT / "static" / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
