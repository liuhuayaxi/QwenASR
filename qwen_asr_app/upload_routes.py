"""Upload submission and progress subscriptions for durable ASR jobs."""
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from .settings import normalize_asr_params, DEFAULT_ASR_PARAMS


def upload_router(jobs, store, require_recording_available):
    router = APIRouter()
    @router.get("/api/upload-jobs/{job_id}")
    async def upload_job_status(job_id: str):
        job = jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "没有找到上传任务")
        return jobs.snapshot(job)


    @router.get("/api/upload-jobs/{job_id}/events")
    async def upload_job_events(job_id: str):
        if job_id not in jobs.jobs:
            raise HTTPException(404, "没有找到上传任务")
        return StreamingResponse(jobs.stream(job_id), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


    @router.post("/api/upload-jobs")
    @router.post("/api/transcribe-file")
    async def transcribe_file(
        file: UploadFile = File(...),
        config_json: str = Form("{}"),
        background: bool = False,
    ):
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
                temp.flush()
                os.fsync(temp.fileno())
            original_filename = file.filename or "录音"
            job = jobs.create(temp_path, original_filename, config)
            if background:
                return JSONResponse(jobs.snapshot(job), status_code=202)
            return StreamingResponse(
                jobs.stream(job["id"]),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        except BaseException:
            if "temp_path" in locals():
                temp_path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()


    @router.post("/api/recordings/{session_id}/resume")
    async def resume_batch_recording(
        session_id: str, config: dict[str, Any] = Body(...)
    ) -> StreamingResponse:
        record = store.find(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="没有找到这条批量录音")
        if record.metadata.get("kind") != "upload":
            raise HTTPException(status_code=400, detail="只有上传录音支持批量续转")
        if record.metadata.get("course_stream"):
            raise HTTPException(status_code=409, detail="此课程仍有分段下载进度，请在课程回放页面重新勾选同一课次继续")
        if record.metadata.get("status") == "completed":
            raise HTTPException(status_code=400, detail="这条录音已经处理完成")
        require_recording_available(session_id)
        try:
            config["asr_params"] = normalize_asr_params(
                config.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"]
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        job = jobs.create(None, str(record.metadata.get("source_filename") or record.metadata.get("title") or "录音"), config, record)
        return StreamingResponse(jobs.stream(job["id"]), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
    return router
