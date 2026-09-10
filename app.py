from __future__ import annotations

import asyncio
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from qwen_asr_app.batch_pipeline import SegmentCorrectionPipeline
from qwen_asr_app.ai_secrets import ai_secret_store
from qwen_asr_app.audio import decode_audio_file, vad_split_ranges
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
    context_window_for,
    generate_recording_title,
    output_limit_for,
    polish_and_translate,
    polish_timestamped_segments,
    polish_timestamped_segments_resilient,
    summarize_transcript,
    test_ai_connection,
)
from qwen_asr_app.note_content import compose_push, summary_from_archive, transcript_blocks
from qwen_asr_app.live_session import TranscriptionSession
from qwen_asr_app.ideashell_sync import ideashell_manager
from qwen_asr_app.model_runtime import ModelManager
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

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
MODEL_PATH = Path(
    os.getenv("QWEN_ASR_MODEL_PATH", ROOT / "models" / "Qwen3-ASR-1.7B-MLX-bf16")
).expanduser()
if not MODEL_PATH.is_absolute():
    MODEL_PATH = (ROOT / MODEL_PATH).resolve()
model_manager = ModelManager(MODEL_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await course_stream_manager.close()
    tasks = [*background_summary_tasks.values(), *background_ai_tasks.values(), *finalization_tasks.values()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    model_manager.cancel_unload()
    await asyncio.to_thread(model_manager._stop_worker_sync)


app = FastAPI(title="Qwen3-ASR 本地语音工作台", lifespan=lifespan)
record_operations: set[str] = set()
summary_jobs: dict[str, dict[str, Any]] = {}
background_summary_tasks: dict[str, asyncio.Task[Any]] = {}
background_ai_tasks: dict[str, asyncio.Task[Any]] = {}
finalization_tasks: dict[str, asyncio.Task[Any]] = {}
live_sessions: dict[str, TranscriptionSession] = {}
segment_retry_locks: dict[tuple[str, int], asyncio.Lock] = {}
pause_requested_tasks: set[str] = set()


def recording_operation(function):
    @wraps(function)
    async def guarded(session_id: str, *args, **kwargs):
        if session_id in record_operations or session_id in running_batch_ids:
            raise HTTPException(status_code=409, detail="这条录音正在处理，请完成后再操作")
        record_operations.add(session_id)
        is_summary = function.__name__ in {"generate_library_summary", "generate_library_summary_abstract"}
        if is_summary:
            summary_jobs[session_id] = {"task": asyncio.current_task(), "started": time.monotonic()}
        try:
            if is_summary:
                return await asyncio.wait_for(function(session_id, *args, **kwargs), timeout=1800)
            return await function(session_id, *args, **kwargs)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="生成耗时超过 30 分钟，已释放任务；重新生成会复用已完成分块") from exc
        except asyncio.CancelledError as exc:
            if is_summary:
                raise HTTPException(status_code=409, detail="已取消总结生成，已完成分块保留，下次可继续") from exc
            raise
        finally:
            if is_summary:
                summary_jobs.pop(session_id, None)
            record_operations.discard(session_id)
    return guarded


def _remember_ai_keys(config: Any) -> None:
    if not isinstance(config, dict):
        return
    ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
    values: dict[str, str] = {"sentence": str(ai.get("api_key") or "")}
    for mode in ("review", "summary", "correction", "global"):
        provider = ai.get(f"{mode}_provider")
        values[mode] = str(provider.get("api_key") or "") if isinstance(provider, dict) else ""
    if any(value.strip() for value in values.values()):
        ai_secret_store.update(values)


@app.get("/api/ai/secrets")
async def ai_secret_status() -> dict[str, Any]:
    return {"saved": ai_secret_store.status(), "storage": "local_user_only"}


@app.put("/api/ai/secrets")
async def save_ai_secrets(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"saved": ai_secret_store.update(payload.get("keys"))}
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
    _remember_ai_keys(config)
    try:
        return await asyncio.wait_for(test_ai_connection(config, str(payload.get("mode") or "")), timeout=45)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="45 秒内未完成测试，请检查网络、服务地址或模型响应速度") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/recordings/{session_id}/summary-status")
async def summary_status(session_id: str) -> dict[str, Any]:
    job = summary_jobs.get(session_id)
    record = recording_store.find(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="没有找到这条录音")
    draft = record.metadata.get("ai_note_summary_draft") or {}
    saved_job = record.metadata.get("summary_job") or {}
    return {"running": bool(job) or session_id in background_summary_tasks,
            "status": saved_job.get("status", ""), "error": saved_job.get("error", ""),
            "completed_parts": len(draft.get("parts", [])),
            "total_parts": draft.get("total_parts", 0), "phase": draft.get("phase", ""),
            "elapsed_seconds": round(time.monotonic() - job["started"]) if job else 0,
            "has_summary": bool(record.metadata.get("ai_note_summary", {}).get("summary"))}


@app.post("/api/recordings/{session_id}/cancel-summary")
async def cancel_summary(session_id: str) -> dict[str, Any]:
    record = recording_store.find(session_id)
    background = background_summary_tasks.get(session_id)
    if background:
        background.cancel()
        return {"cancelled": True}
    job = summary_jobs.get(session_id)
    if job:
        job["task"].cancel()
        return {"cancelled": True}
    # The recording tail may still be saving while the automatic summary is
    # queued.  Cancel only the AI intent, never the audio finalization.
    saved = record.metadata.get("summary_job") if record else None
    if isinstance(saved, dict) and saved.get("status") in {"queued", "waiting_retry", "paused", "failed", "interrupted"}:
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
    "summary": "课后 AI 总结",
    "refinement": "未精修内容",
    "live_summary": "实时累计总结",
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
    _saved_ai_job(record, kind).update(values, updated_at=local_now())
    record.save_metadata()


def _task_error_kind(error: str) -> str:
    value = str(error or "").lower()
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


AUTO_RETRY_KINDS = {"rate_limit", "provider_busy"}
MAX_AUTOMATIC_RETRIES = 6


def _error_kind(exc: BaseException) -> str:
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


def compact_ai_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for record in recording_store.list_records():
        base = {
            "recording_id": record.id,
            "recording_title": str(record.metadata.get("title") or "未命名录音"),
            "category": str(record.metadata.get("category") or "未分类"),
        }
        summary = record.metadata.get("summary_job") or {}
        if isinstance(summary, dict) and summary.get("status") in {"queued", "running", "waiting_retry", "paused", "failed", "interrupted"}:
            draft = record.metadata.get("ai_note_summary_draft") or {}
            status = str(summary.get("status"))
            tasks.append({
                **base,
                "id": f"summary:{record.id}",
                "type": "summary",
                "label": AI_TASK_LABELS["summary"],
                "status": status,
                "error": str(summary.get("error") or ""),
                "error_kind": _task_error_kind(str(summary.get("error") or "")),
                "completed": len(draft.get("parts") or []),
                "total": int(draft.get("total_parts") or 0),
                "phase": str(draft.get("phase") or ""),
                "retry_in_seconds": int(summary.get("retry_in_seconds") or 0),
                "auto_retry_count": int(summary.get("auto_retry_count") or 0),
                "actions": (["pause", "cancel"] if status in {"queued", "running", "waiting_retry"} else ["continue", "retry", "cancel"]),
                "updated_at": str(summary.get("updated_at") or summary.get("started_at") or ""),
            })

        refinement_job = _saved_ai_job(record, "refinement")
        missing_blocks = [block for block in transcript_blocks(record) if not block["refined"]]
        refinement_running = f"refinement:{record.id}" in background_ai_tasks
        live_session = live_sessions.get(record.id)
        if live_session and (
            live_session.periodic_tasks.get("review")
            and not live_session.periodic_tasks["review"].done()
        ):
            refinement_running = True
        refinement_ready = record.metadata.get("status") not in {"active", "processing"}
        refinement_cancelled = refinement_job.get("status") == "cancelled" and not refinement_running
        if not refinement_cancelled and (refinement_running or refinement_job.get("status") in {"queued", "running", "waiting_retry", "paused", "failed", "interrupted"} or (refinement_ready and missing_blocks)):
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
            if status != "completed":
                task_error = str(refinement_job.get("error") or (failed_errors[-1] if failed_errors else ""))
                tasks.append({
                    **base,
                    "id": f"refinement:{record.id}",
                    "type": "refinement",
                    "label": AI_TASK_LABELS["refinement"],
                    "status": status,
                    "error": task_error,
                    "error_kind": str(refinement_job.get("error_kind") or _task_error_kind(task_error)),
                    "completed": int(refinement_job.get("completed") or 0),
                    "total": int(refinement_job.get("total") or len(missing_blocks)),
                    "missing_blocks": len(missing_blocks),
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
                error = str(live_job.get("error") or (failed or {}).get("ai_error") or "")
                failed_end = int(round(float((failed or {}).get("end", 0))))
                tasks.append({
                    **base,
                    "id": f"live_summary:{record.id}",
                    "type": "live_summary",
                    "label": AI_TASK_LABELS["live_summary"],
                    "status": status,
                    "error": error,
                    "error_kind": str(live_job.get("error_kind") or (failed or {}).get("error_kind") or _task_error_kind(error)),
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
    _update_ai_job(
        record, kind, status="queued", error="", error_kind="", started_at=local_now(),
        completed=0, auto_retry_count=0, retry_in_seconds=0, pause_requested=False,
    )

    async def run() -> None:
        automatic_attempt = 0
        try:
            while True:
                try:
                    _update_ai_job(record, kind, status="running", retry_in_seconds=0)
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
                status="paused" if paused else "interrupted",
                error="任务已暂停；已完成内容保留，可继续" if paused else "任务已取消；已完成内容保留，可继续",
                error_kind="paused" if paused else "cancelled",
                retry_in_seconds=0,
                pause_requested=False,
            )
            raise
        finally:
            background_ai_tasks.pop(task_id, None)

    task = asyncio.create_task(run())
    background_ai_tasks[task_id] = task
    return True


def _unrefined_segments(record: RecordingRecord) -> list[dict[str, Any]]:
    ids = {
        int(value)
        for block in transcript_blocks(record)
        if not block["refined"]
        for value in block.get("segment_ids", [])
    }
    return [segment for segment in record.segments if int(segment.get("id", 0)) in ids]


_EMPTY_SPEECH_RE = re.compile(r"^[\s，。！？,.!?、]*(?:啊|哎|诶|嗯|呃|额|唔|那个|就是说)[\s，。！？,.!?、]*$")


async def _run_refinement_job_unlocked(record: RecordingRecord, config: dict[str, Any]) -> None:
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
        return
    context_window = context_window_for(config, "correction")
    output_budget = output_limit_for(config, "correction", 32_768)
    token_budget = max(
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
    previous_position = -2
    positions = {int(segment.get("id", 0)): index for index, segment in enumerate(record.segments)}
    for segment in targets:
        position = positions[int(segment.get("id", 0))]
        cost = estimate_tokens(str(segment.get("raw") or "")) + 24
        if current and (
            position != previous_position + 1
            or current_tokens + cost > token_budget
            or len(current) >= 10
        ):
            groups.append(current)
            current, current_tokens = [], 0
        current.append(segment)
        current_tokens += cost
        previous_position = position
    if current:
        groups.append(current)
    _update_ai_job(record, "refinement", total=len(groups), completed=0)
    for index, group in enumerate(groups):
        first_position = positions[int(group[0]["id"])]
        history = "\n".join(
            str(item.get("corrected") or item.get("raw") or "")
            for item in record.segments[:first_position]
        )
        group_config = json.loads(json.dumps(config))
        history_chars = int((group_config.get("ai") or {}).get("history_chars") or 4000)
        group_config["_transcript_context"] = history[-max(0, min(history_chars, 20_000)):]
        mapped = await polish_timestamped_segments_resilient(
            group,
            str(group[0].get("language") or ""),
            group_config,
            mode="correction",
        )
        group_ids = {int(item["id"]) for item in group}
        reviews = [
            item for item in record.metadata.get("ai_reviews") or []
            if isinstance(item, dict) and not group_ids.intersection(int(value) for value in item.get("segment_ids", []))
        ]
        by_id = {int(item["id"]): item for item in group}
        for part_index, part in enumerate(mapped):
            members = [by_id[value] for value in part["segment_ids"]]
            reviews.append({
                "id": f"manual-{int(time.time())}-{index}-{part_index}",
                "start": min(float(member.get("start", 0)) for member in members),
                "end": max(float(member.get("end", 0)) for member in members),
                "segment_ids": part["segment_ids"],
                "raw": "\n\n".join(str(member.get("raw") or "") for member in members),
                "corrected": part["corrected"],
                "manual_refinement": True,
            })
        record.metadata["ai_reviews"] = reviews
        for segment in record.segments:
            if int(segment.get("id", 0)) in group_ids:
                segment.pop("ai_error", None)
                segment["ai_pending"] = False
        record.save_all()
        _update_ai_job(record, "refinement", completed=index + 1, total=len(groups))
    if record.metadata.get("status") == "completed":
        await asyncio.to_thread(record.make_bundle)


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
    if record.metadata.get("status") == "completed":
        await asyncio.to_thread(record.make_bundle)


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
    if kind not in AI_TASK_LABELS or action not in {"retry", "continue", "pause", "cancel"}:
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
                status="interrupted",
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
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="请提供当前 AI 配置后重试")
    _remember_ai_keys(config)
    config = json.loads(json.dumps(config))
    if kind == "refinement" and record.metadata.get("status") in {"active", "processing"}:
        raise HTTPException(status_code=409, detail="请先结束当前录音，再重新精修尚未完成的段落")
    course = record.metadata.get("course_settings") or {}
    config.setdefault("terminology", course.get("terminology") or [])
    if action == "retry":
        if kind == "summary":
            record.metadata.pop("ai_note_summary_draft", None)
        elif kind == "live_summary":
            record.metadata["ai_summaries"] = []
        else:
            record.metadata["ai_reviews"] = [
                item
                for item in record.metadata.get("ai_reviews") or []
                if not isinstance(item, dict) or not item.get("manual_refinement")
            ]
        record.save_all()
    if kind == "summary":
        summary_payload = payload.get("summary_payload")
        if not isinstance(summary_payload, dict):
            summary_payload = {"config": config}
        else:
            summary_payload = {**summary_payload, "config": config}
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


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": model_manager.status,
        "detail": model_manager.detail,
        "device": model_manager.device,
        "load_seconds": model_manager.load_seconds,
        "model": "Qwen3-ASR-1.7B BF16 (MLX)",
        "model_path": str(MODEL_PATH),
    }


@app.get("/api/config")
async def public_config() -> dict[str, Any]:
    saved_keys = ai_secret_store.status()
    return {
        "feature_version": "ai-task-center-v11",
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
async def recording_library() -> dict[str, Any]:
    records = recording_store.list_records()
    return {
        **app_settings.public(),
        "records": [record.public(include_segments=False) for record in records],
    }


@app.put("/api/library/settings")
async def update_library_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        requested_root = str(payload.get("storage_root") or "")
        migrate = payload.get("migrate", True) is not False
        migration: dict[str, Any] = {
            "source_root": str(app_settings.storage_root),
            "storage_root": str(app_settings.storage_root),
            "migrated_records": 0,
            "migrated_files": 0,
            "skipped": [],
        }
        if migrate:
            requested_path = Path(requested_root).expanduser()
            same_root = requested_path.is_absolute() and requested_path.resolve() == app_settings.storage_root.resolve()
            records = recording_store.list_records()
            busy = [
                record
                for record in records
                if record.metadata.get("status") in {"active", "processing"}
            ]
            if busy and not same_root:
                raise ValueError("请先结束当前录音或等待上传转写完成，再迁移资料库")
            migration = app_settings.migrate_storage_root(requested_root)
        else:
            app_settings.set_storage_root(requested_root)
        raw_categories = payload.get("categories")
        if isinstance(raw_categories, list):
            for category in raw_categories:
                app_settings.add_category(str(category))
        recording_store.refresh()
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


@app.post("/api/library/categories")
async def create_library_category(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分类名称不能为空")
    return {"category": app_settings.add_category(name), **app_settings.public()}


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


@app.post("/api/recordings/{session_id}/generate-title")
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
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="AI 配置格式无效")
    prompt = str(payload.get("title_prompt") or DEFAULT_AI_TITLE_PROMPT).strip()
    try:
        title = await generate_recording_title(
            summary,
            prompt,
            config,
            current_title=str(record.metadata.get("title") or ""),
            category=str(record.metadata.get("category") or ""),
        )
        return {"title": title, "summary": summary, "summary_source": "ai"}
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
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="AI 配置格式无效")
    _remember_ai_keys(config)
    config = json.loads(json.dumps(config))
    config["_note_summary_system_prompt"] = str(
        payload.get("summary_system_prompt") or DEFAULT_NOTE_SUMMARY_PROMPT
    ).strip()[:30_000]
    config["_note_summary_custom_prompt"] = str(
        payload.get("custom_prompt") or ""
    ).strip()[:20_000]
    config["_note_abstract_system_prompt"] = str(
        payload.get("abstract_system_prompt") or DEFAULT_NOTE_ABSTRACT_PROMPT
    ).strip()[:20_000]
    try:
        summary = await build_note_summary(record, config)
        if record.metadata.get("status") == "completed":
            record.make_bundle()
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
    if session_id in record_operations or session_id in running_batch_ids:
        raise HTTPException(status_code=409, detail="这条录音正在处理，请完成后再生成笔记")
    # Only progress is persisted; credentials remain in this task's memory.
    record.metadata["summary_job"] = {
        "status": "queued", "automatic": automatic, "started_at": local_now(), "error": "",
        "auto_retry_count": 0, "retry_in_seconds": 0, "pause_requested": False,
    }
    record.save_metadata()

    async def run() -> None:
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
                    await generate_library_summary(session_id, payload)
                    save_state(
                        "completed", auto_retry_count=automatic_attempt,
                        retry_in_seconds=0,
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
                "paused" if paused else "interrupted",
                "生成已暂停，已完成内容保留，可以继续" if paused else "生成已中断，已完成内容保留，可以重新生成",
                error_kind="paused" if paused else "cancelled",
                retry_in_seconds=0,
                pause_requested=False,
            )
            raise
        finally:
            background_summary_tasks.pop(session_id, None)

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
                        status="paused" if paused else "interrupted",
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
    await session.pause(send_status=False)
    if record.metadata.get("pending_segments") and not session.tasks:
        session.recovered = False
    await session.recover_pending()
    record.metadata["finalization"] = {"status": "running", "started_at": local_now(), "error": ""}
    # Persist intent before waiting for ASR. If the service stops, the browser
    # can resubmit the current provider settings without storing any API keys.
    record.metadata["summary_job"] = {"status": "queued", "automatic": True, "error": ""}
    record.save_metadata()

    async def finish() -> None:
        try:
            await session.drain()
            if record.metadata.get("pending_segments"):
                raise RuntimeError("部分录音尚未成功转写，音频已保留，请重试结束保存")
            completion = asyncio.create_task(asyncio.to_thread(record.complete))
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                # A running encoder thread cannot be cancelled. Let its disk
                # writes finish before persisting the interrupted job state.
                await completion
                raise
            record.metadata["finalization"].update(status="completed", updated_at=local_now())
            record.save_metadata()
            if recording_store.active is record:
                recording_store.active = None
            summary_intent = record.metadata.get("summary_job") or {}
            if not summary_intent.get("cancel_requested"):
                start_summary_job(record, summary_payload, automatic=True)
            await session.send({"type": "ended", "session": record.public()})
            session.recording = None
            live_sessions.pop(record.id, None)
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

    finalization_tasks[record.id] = asyncio.create_task(finish())
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
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="AI 配置格式无效")
    config = json.loads(json.dumps(config))
    config["_note_abstract_system_prompt"] = str(
        payload.get("abstract_system_prompt") or DEFAULT_NOTE_ABSTRACT_PROMPT
    ).strip()[:20_000]
    try:
        summary = await regenerate_note_abstract(record, config)
        if record.metadata.get("status") == "completed":
            record.make_bundle()
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
    try:
        summary = record.update_note_summary(str(payload.get("summary") or ""))
        if record.metadata.get("status") == "completed":
            record.make_bundle()
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
    return value


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
async def ideashell_status(server_url: str) -> dict[str, Any]:
    try:
        return ideashell_manager.connection(server_url).status()
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
        return compose_push(record, str(payload.get("content_mode") or "combined"))
    except ValueError as exc:
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
        archive = compose_push(record, str(payload.get("content_mode") or "combined"))
        markdown = archive["content"]
        if payload.get("preview_digest") and payload["preview_digest"] != archive["digest"]:
            raise ValueError("内容已更新，请重新预览后推送")
        sync = record.metadata.get("ideashell_sync")
        sync = dict(sync) if isinstance(sync, dict) else {}
        note_id = str(sync.get("note_id") or "")
        server_url = sync.get("server_url") if note_id else payload.get("server_url")
        connection = ideashell_manager.connection(
            str(server_url or payload.get("server_url") or "")
        )
        title = str(record.metadata.get("title") or "未命名录音")
        excerpt = str(summary.get("abstract") or "").strip() if isinstance(summary, dict) else ""
        excerpt = excerpt[:100] or record._compact_summary(markdown, 180)
        folder_id = await ensure_ideashell_course_folder(
            connection, str(record.metadata.get("category") or "未分类")
        )
        if note_id:
            result = await connection.call_tool(
                "note_update",
                {"note_id": note_id, "title": title, "content": markdown, "summary": excerpt},
            )
            await connection.call_tool(
                "note_move", {"note_ids": [note_id], "folder_id": folder_id}
            )
        else:
            result = await connection.call_tool(
                "note_create",
                {
                    "title": title,
                    "content": markdown,
                    "summary": excerpt,
                    "tag": "QwenASR",
                    "folder_id": folder_id,
                },
            )
            note_id = ideashell_note_id(result)
            if not note_id:
                raise RuntimeError("IdeaShell 未返回笔记编号，请先在 IdeaShell 确认创建结果，避免重复推送")
        sync.update(
            {
                "note_id": note_id,
                "server_url": connection.server_url,
                "last_pushed_at": local_now(),
                "status": "synced",
                "folder_id": folder_id,
                "folder_name": str(record.metadata.get("category") or "未分类"),
                "content_mode": archive["mode"],
                "content_digest": archive["digest"],
            }
        )
        record.metadata["ideashell_sync"] = sync
        record.save_metadata()
        return {"synced": True, "direction": "push", "note_id": note_id, "session": record.public(include_segments=False)}
    except (ValueError, RuntimeError, ImportError, ModuleNotFoundError) as exc:
        print(f"IdeaShell 推送失败 recording={session_id}: {exc}", flush=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        print(f"IdeaShell 推送异常 recording={session_id}: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"IdeaShell 推送失败：{exc}") from exc


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
        result = await connection.call_tool("note_detail", {"note_id": note_id, "scope": "brief"})
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
            if record.metadata.get("status") == "completed":
                await asyncio.to_thread(record.make_bundle)
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
async def download_recording(session_id: str, kind: str) -> FileResponse:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这次录音")
    if record.metadata.get("status") != "completed":
        raise HTTPException(status_code=409, detail="请先结束并保存录音，再下载文件")
    if kind == "bundle":
        path = record.make_bundle()
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
        record.save_summary_markdown()
        path = record.ai_summary_path
        filename = f"{safe_component(str(record.metadata.get('title') or session_id), session_id)}-AI总结.md"
        media_type = "text/markdown; charset=utf-8"
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
    return FileResponse(path, filename=filename, media_type=media_type)


def stream_event(event_type: str, **payload: Any) -> bytes:
    return (json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n").encode("utf-8")


running_batch_ids: set[str] = set()

from qwen_asr_app.course_stream import CourseStreamManager, course_stream_router
course_stream_manager = CourseStreamManager(model_manager, recording_store, app_settings, running_batch_ids)
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
):
    record: RecordingRecord | None = existing_record
    pipeline = None
    warmup = None
    inference_task = None
    model_manager.active_jobs += 1
    model_manager.cancel_unload()
    started_processing = time.monotonic()
    try:
        warmup = asyncio.create_task(model_manager.load())
        source_path = temp_path
        if record is not None:
            source_path = record.audio_path
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

        if record is None:
            title = str(config.get("title") or Path(original_filename).stem or "上传录音").strip()
            category = str(config.get("category") or "未分类")
            yield stream_event("progress", progress=7, stage="正在归档为 AAC", duration=round(duration, 1))
            record = await asyncio.to_thread(
                recording_store.create_upload,
                source_path,
                original_filename,
                duration,
                title,
                category,
            )
        running_batch_ids.add(record.id)
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
        record.metadata["batch_options"] = redacted_batch_config(config)
        record.metadata.setdefault("ai_groups", [])
        corrupt_segments = [
            segment for segment in record.segments if has_repetitive_tail(str(segment.get("raw") or ""))
        ]
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

        ranges = deque(vad_split_ranges(audio, start_sample=start_sample))
        estimated_total = len(record.segments) + len(ranges)
        while ranges:
            segment_start_sample, segment_end_sample = ranges.popleft()
            block = audio[segment_start_sample:segment_end_sample]
            block_duration = len(block) / SAMPLE_RATE
            max_tokens = min(1_536, max(256, int(block_duration * 10) + 128))
            try:
                inference_task = asyncio.create_task(model_manager.transcribe(
                    block,
                    config.get("language"),
                    asr_terminology_context(
                        str(config.get("context", "")), config.get("terminology")
                    ),
                    max_tokens=max_tokens,
                    asr_params=asr_params_with_terminology(
                        config.get("asr_params"), config.get("terminology")
                    ),
                    timeout_seconds=180,
                ))
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
                if has_repetitive_tail(raw):
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
                "latency": seconds,
                "duration": round(block_duration, 1),
                "start": round(segment_start_sample / SAMPLE_RATE, 2),
                "end": round(segment_end_sample / SAMPLE_RATE, 2),
                "reason": "upload_vad_block",
                "ai_pending": bool(config.get("ai_correction"))
                or config.get("translation", "off") != "off",
            }
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
        record.metadata["asr_seconds"] = round(sum(float(s.get("latency", 0)) for s in record.segments), 2)
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
        if inference_task and not inference_task.done():
            await asyncio.gather(inference_task, return_exceptions=True)
        if warmup:
            await asyncio.gather(warmup, return_exceptions=True)
        model_manager.active_jobs = max(0, model_manager.active_jobs - 1)
        model_manager.schedule_unload()
        if record:
            running_batch_ids.discard(record.id)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@app.post("/api/transcribe-file")
async def transcribe_file(
    file: UploadFile = File(...),
    config_json: str = Form("{}"),
) -> StreamingResponse:
    try:
        config = json.loads(config_json)
        if not isinstance(config, dict):
            raise ValueError
        config["asr_params"] = normalize_asr_params(
            config.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"]
        )
    except (json.JSONDecodeError, ValueError) as exc:
        detail = str(exc) or "转写配置不是有效的 JSON 对象"
        raise HTTPException(status_code=400, detail=detail) from exc

    suffix = Path(file.filename or "recording").suffix.lower()[:12] or ".audio"
    total_bytes = 0
    try:
        with tempfile.NamedTemporaryFile(prefix="qwen-asr-", suffix=suffix, delete=False) as temp:
            temp_path = Path(temp.name)
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="录音文件不能超过 2GB")
                temp.write(chunk)
        original_filename = file.filename or "录音"
        return StreamingResponse(
            batch_transcription_stream(temp_path, original_filename, config),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    except Exception:
        if "temp_path" in locals():
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@app.post("/api/recordings/{session_id}/resume")
async def resume_batch_recording(
    session_id: str, config: dict[str, Any] = Body(...)
) -> StreamingResponse:
    record = recording_store.find(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="没有找到这条批量录音")
    if record.metadata.get("kind") != "upload":
        raise HTTPException(status_code=400, detail="只有上传录音支持批量续转")
    if record.metadata.get("course_stream"):
        raise HTTPException(status_code=409, detail="此课程仍有分段下载进度，请在课程回放页面重新勾选同一课次继续")
    if record.metadata.get("status") == "completed":
        raise HTTPException(status_code=400, detail="这条录音已经处理完成")
    if session_id in running_batch_ids:
        raise HTTPException(status_code=409, detail="这条录音当前已经在处理中")
    try:
        config["asr_params"] = normalize_asr_params(
            config.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"]
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    running_batch_ids.add(session_id)

    async def guarded_stream():
        try:
            async for event in batch_transcription_stream(
                None,
                str(record.metadata.get("source_filename") or record.metadata.get("title") or "录音"),
                config,
                existing_record=record,
            ):
                yield event
        finally:
            running_batch_ids.discard(session_id)

    return StreamingResponse(
        guarded_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.websocket("/ws/transcribe")
async def transcribe_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    model_manager.client_connected()
    active = recording_store.active
    session = live_sessions.get(active.id) if active is not None else None
    if session is None:
        session = TranscriptionSession(websocket, model_manager, recording_store)
        session.recording = active
        if active is not None:
            live_sessions[active.id] = session
    session.ws = websocket
    await session.send({"type": "ready", "model_status": model_manager.status})
    try:
        while True:
            message = await websocket.receive()
            if session.ws is not websocket:
                break
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.ingest(message["bytes"])
                continue
            text = message.get("text")
            if not text:
                continue
            payload = json.loads(text)
            kind = payload.get("type")
            if kind == "config":
                _remember_ai_keys(payload)
                pending = session.update_config(payload)
                await session.send({"type": "configured", "pending": pending})
            elif kind == "start":
                try:
                    await session.start_or_resume()
                    if session.recording is not None:
                        live_sessions[session.recording.id] = session
                except (ValueError, OSError) as exc:
                    await session.send({"type": "start_error", "message": str(exc)})
            elif kind in {"pause", "stop"}:
                await session.pause()
            elif kind == "end":
                try:
                    summary_payload = payload.get("summary_payload")
                    if not isinstance(summary_payload, dict):
                        summary_payload = {"config": session.config}
                    await begin_recording_end(session, summary_payload)
                except (ValueError, OSError) as exc:
                    await session.send({"type": "end_error", "message": str(exc)})
            elif kind == "ping":
                await session.send({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        if session.ws is websocket and session.recording is not None:
            await session.pause(send_status=False)
        model_manager.client_disconnected()


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        ROOT / "static" / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
