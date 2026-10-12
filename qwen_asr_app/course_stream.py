"""Durable course-audio ingestion with overlapping download, ASR and correction."""
from __future__ import annotations

import asyncio
import hashlib
import json
import wave
import time
import shutil
from collections import deque
from pathlib import Path

from .lazy import lazy_module
from fastapi import APIRouter, HTTPException, Request

from .course_identity import match_record, supplement
from .ai_preferences import ai_preferences
from .durable import atomic_bytes, atomic_json
from .batch_ai import segment_ai_complete
from .batch_pipeline import SegmentCorrectionPipeline
from .review import bisect_audio_range, has_repetitive_tail, trim_repetitive_tail
from .settings import SAMPLE_RATE, normalize_asr_params, DEFAULT_ASR_PARAMS
from .storage import RecordingRecord, local_now
from .terminology import asr_params_with_terminology, asr_terminology_context

np = lazy_module("numpy")


def public_config(config):
    saved = json.loads(json.dumps(config))
    ai = saved.get("ai") or {}
    ai.pop("api_key", None)
    for value in ai.values():
        if isinstance(value, dict):
            value.pop("api_key", None)
    saved["ai"] = ai
    return saved


class CourseStreamJob:
    def __init__(self, record, config, model, busy_ids):
        self.record, self.config, self.model, self.busy_ids = record, config, model, busy_ids
        self.state = record.metadata["course_stream"]
        self.event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.task = None

    def recover_chunks(self):
        folder = self.record.directory / ".course-chunks"
        chunks = self.state["chunks"]
        if self.record.metadata.get("status") == "completed":
            return
        # A sidecar is the commit record if the process died before session.json.
        while (folder / f"{len(chunks):05}.json").exists():
            item = json.loads((folder / f"{len(chunks):05}.json").read_text())
            raw = (folder / f"{len(chunks):05}.pcm").read_bytes()
            if len(raw) != item["samples"] * 2 or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("音频分段校验失败，已保留现场文件")
            chunks.append(item)
        for index, item in enumerate(chunks):
            raw = (folder / f"{index:05}.pcm").read_bytes()
            if len(raw) != item["samples"] * 2 or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("音频分段校验失败，已保留现场文件")
        self.record.metadata["sample_count"] = sum(c["samples"] for c in chunks)

    def start(self):
        self.recover_chunks()
        self.record.metadata.update(status="processing", batch_options=public_config(self.config))
        self.state.pop("error", None)
        self.state["paused"] = False
        self.state.setdefault("started_at", time.time())
        self.state["stage"] = "等待音频分段"
        self.record.save_all()
        self.busy_ids.add(self.record.id)
        self.task = asyncio.create_task(self.run())

        def cleanup(done):
            # A cancellation before run() starts must still release ownership.
            if self.task is done and done.cancelled() and self.record.id in self.busy_ids:
                self.state.update(stage="interrupted", error="任务已停止，分段与转写已保留")
                self.record.metadata["status"] = "interrupted"
                self.record.save_all()
                self.busy_ids.discard(self.record.id)

        self.task.add_done_callback(cleanup)

    def snapshot(self):
        r, s = self.record, self.state
        chunks = s["chunks"]
        corrected = sum(segment_ai_complete(v, self.config) for v in r.segments)
        done = int(s.get("completed_chunks", 0))
        return {"id": r.id, "directory": str(r.directory), "status": r.metadata["status"],
                "next_index": len(chunks), "download_seconds": round(r.duration, 2),
                "expected_seconds": round(float(s.get("expected_seconds", 0)), 2),
                "asr_seconds": round(sum(c["samples"] for c in chunks[:done]) / SAMPLE_RATE, 2),
                "segments": len(r.segments), "corrected_segments": corrected,
                "ai_errors": sum(bool(v.get("ai_error")) for v in r.segments),
                "download_complete": bool(s.get("input_complete")), "stage": s.get("stage", ""),
                "error": s.get("error", ""), "paused": bool(s.get("paused")),
                "ai_pending": sum(bool(v.get("ai_pending")) for v in r.segments),
                "metrics": {"first_text_seconds": s.get("first_text_seconds"),
                            "download_wall_seconds": s.get("download_wall_seconds"),
                            "asr_queue_seconds": round(sum(v.get("metrics", {}).get("queue_seconds", 0) for v in r.segments), 3),
                            "asr_inference_seconds": round(sum(v.get("metrics", {}).get("inference_seconds", 0) for v in r.segments), 3),
                            "archive_seconds": s.get("archive_seconds")},
                "resume": chunks[-1].get("cursor") if chunks else None}

    async def accept(self, index, raw, cursor):
        if not raw or len(raw) % 2 or len(raw) > 16000 * 2 * 121:
            raise HTTPException(400, "音频分段需为不超过 120 秒的单声道 16 kHz PCM")
        digest = hashlib.sha256(raw).hexdigest()
        async with self.lock:
            chunks = self.state["chunks"]
            if index < len(chunks):
                if chunks[index]["sha256"] != digest:
                    raise HTTPException(409, "此分段已经保存，但内容不同；请重新连接导入任务")
                return self.snapshot()
            if index != len(chunks) or self.state.get("input_complete"):
                raise HTTPException(409, "音频分段顺序不连续或接收已经结束")
            if self.task is None or self.task.done():
                raise HTTPException(409, self.state.get("error") or "请重新选择课次继续")
            samples = len(raw) // 2
            if self.record.duration + samples / SAMPLE_RATE > 7205:
                raise HTTPException(400, "课次超过 2 小时")
            if shutil.disk_usage(self.record.directory).free < 128 * 1024**2:
                raise HTTPException(507, "磁盘剩余空间不足，已保存进度；清理空间后可继续")
            folder = self.record.directory / ".course-chunks"
            folder.mkdir(exist_ok=True)
            target = folder / f"{index:05}.pcm"
            item = {"samples": samples, "sha256": digest, "cursor": cursor, "received_at": time.time()}
            atomic_bytes(target, raw)
            atomic_json(target.with_suffix(".json"), item)
            chunks.append(item)
            self.record.metadata["sample_count"] = sum(c["samples"] for c in chunks)
            self.record.save_metadata()
            self.event.set()
            return self.snapshot()

    async def finish_input(self, count):
        async with self.lock:
            if count != len(self.state["chunks"]) or count == 0:
                raise HTTPException(409, "音频还未完整接收")
            if self.state.get("input_complete"):
                return self.snapshot()
            if self.task is None or self.task.done():
                raise HTTPException(409, "任务已经暂停或结束，请重新选择课次继续")
            self.state["input_complete"] = True
            self.state.setdefault("download_wall_seconds", round(time.time() - self.state.get("started_at", time.time()), 3))
            self.record.save_metadata()
            self.event.set()
            return self.snapshot()

    def assemble(self):
        # Legacy uploads use the archived AAC as their working path. Rebuilding
        # them needs a separate WAV so encoding and cleanup cannot touch the archive.
        if self.record.working_audio_path == self.record.audio_path:
            self.record.working_audio_path = self.record.directory / '.recording-recovery.wav'
            self.record.metadata['working_audio_filename'] = self.record.working_audio_path.name
        target = self.record.working_audio_path
        temporary = target.with_suffix(".partial.wav")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(SAMPLE_RATE)
            for index, chunk in enumerate(self.state["chunks"]):
                raw = (self.record.directory / ".course-chunks" / f"{index:05}.pcm").read_bytes()
                if hashlib.sha256(raw).hexdigest() != chunk["sha256"]:
                    raise ValueError("已下载音频的完整性校验失败，请重新获取此课次")
                output.writeframes(raw)
        temporary.replace(target)
        self.record.ensure_aac()
        # Retain one lossless local source for a later A -> A+B supplement.
        import soundfile as sf
        cache = self.record.directory / ".course-source.flac"
        temporary_cache = cache.with_suffix(".tmp.flac")
        with sf.SoundFile(str(temporary_cache), mode="w", samplerate=SAMPLE_RATE,
                          channels=1, subtype="PCM_16", format="FLAC") as output:
            for index in range(len(self.state["chunks"])):
                raw = (self.record.directory / ".course-chunks" / f"{index:05}.pcm").read_bytes()
                output.write(np.frombuffer(raw, dtype="<i2"))
        temporary_cache.replace(cache)

    async def run(self):
        pipeline = None
        inference = None
        assembly = None
        asr_owned = True
        self.model.active_jobs += 1
        self.model.cancel_unload()
        try:
            if self.config.get("ai_correction"):
                pipeline = SegmentCorrectionPipeline(self.record, self.config)
                for segment in list(self.record.segments):
                    if not segment_ai_complete(segment, self.config):
                        await pipeline.submit(segment)
            while True:
                index = int(self.state.get("completed_chunks", 0))
                chunks = self.state["chunks"]
                if index >= len(chunks):
                    if self.state.get("input_complete"):
                        break
                    self.state["stage"] = "等待下一段下载 · 已有文字同时精修"
                    self.event.clear()
                    try:
                        await asyncio.wait_for(self.event.wait(), 300)
                    except asyncio.TimeoutError:
                        # Release GPU resources after a lost importer connection.
                        raise ValueError("音频下载暂时中断，已保存分段；重新勾选课次可继续")
                    continue
                raw_bytes = (self.record.directory / ".course-chunks" / f"{index:05}.pcm").read_bytes()
                if hashlib.sha256(raw_bytes).hexdigest() != chunks[index]["sha256"]:
                    raise ValueError("已接收音频校验失败，已停止转写并保留文件")
                audio = np.frombuffer(raw_bytes, dtype="<i2").astype(np.float32) / 32768
                offset = sum(c["samples"] for c in chunks[:index]) / SAMPLE_RATE
                saved_parts = [s for s in self.record.segments if s.get("course_chunk") == index]
                done_sample = max((int(s["chunk_end_sample"]) for s in saved_parts), default=0)
                ranges = deque([(done_sample, len(audio))]) if done_sample < len(audio) else deque()
                part = len(saved_parts)
                while ranges:
                    start, end = ranges.popleft()
                    if getattr(self.model, "realtime_pending", False) and end - start > 25 * SAMPLE_RATE:
                        ranges.appendleft((start + 25 * SAMPLE_RATE, end))
                        end = start + 25 * SAMPLE_RATE
                    part += 1
                    ident = (index + 1) * 100 + part
                    seconds = (end - start) / SAMPLE_RATE
                    self.state["stage"] = f"正在转写第 {index + 1} 段 · 下载与精修同时进行"
                    self.record.save_metadata()
                    metrics = {}
                    inference = asyncio.create_task(self.model.transcribe(
                        audio[start:end], self.config.get("language"),
                        asr_terminology_context(self.config.get("context", ""), self.config.get("terminology")),
                        max_tokens=min(1536, max(256, int(seconds * 10) + 128)),
                        asr_params=asr_params_with_terminology(self.config.get("asr_params"), self.config.get("terminology")),
                        timeout_seconds=180, metrics=metrics,
                    ))
                    try:
                        text, language, latency = await asyncio.shield(inference)
                        if has_repetitive_tail(text):
                            if seconds > 35:
                                raise ValueError("检测到重复输出")
                            text = trim_repetitive_tail(text)
                    except Exception as exc:
                        if seconds > 35 and ("重复" in str(exc) or "超时" in str(exc)):
                            first, second = bisect_audio_range(audio, start, end)
                            ranges.appendleft(second); ranges.appendleft(first)
                            part -= 1
                            continue
                        raise
                    if text:
                        self.state.setdefault("first_text_seconds", round(time.time() - self.state["started_at"], 3))
                        segment = {"id": ident, "raw": text, "language": language, "latency": latency,
                                   "duration": round(seconds, 2), "start": round(offset + start / SAMPLE_RATE, 3),
                                   "end": round(offset + end / SAMPLE_RATE, 3), "reason": "course_stream",
                                   "course_chunk": index, "chunk_end_sample": end, "metrics": metrics,
                                   "ai_pending": bool(pipeline)}
                        if pipeline:
                            segment["ai_requested"] = {"correction": True, "translation": "off"}
                        self.record.upsert_segment(segment)
                        if pipeline:
                            await pipeline.submit(segment)
                self.state["completed_chunks"] = index + 1
                self.record.metadata["batch_progress"] = {
                    "stage": "asr", "completed_seconds": offset + len(audio) / SAMPLE_RATE,
                    "audio_duration": self.state["expected_seconds"],
                    "percent": min(95, 95 * (offset + len(audio) / SAMPLE_RATE) / max(1, self.state["expected_seconds"])),
                    "message": self.state["stage"],
                }
                self.record.save_all()
                if pipeline:
                    while not pipeline.events.empty():
                        pipeline.events.get_nowait()
            # AI correction and AAC archival do not use the ASR model. Release
            # this job's lease after the last inference, while keeping record
            # ownership until every AI writer has finished.
            self.model.active_jobs = max(0, self.model.active_jobs - 1)
            asr_owned = False
            self.model.schedule_unload()
            self.state["stage"] = "语音识别已完成，等待剩余精修并合并归档"
            self.record.save_metadata()
            if pipeline:
                await pipeline.finish()
            if not self.record.segments:
                raise ValueError("没有从音频中识别到文字，下载的音频已保留")
            archive_started = time.monotonic()
            assembly = asyncio.create_task(asyncio.to_thread(self.assemble))
            await asyncio.shield(assembly)
            self.state["archive_seconds"] = round(time.monotonic() - archive_started, 3)
            # No AI worker can write the record while final exports are built.
            self.record.metadata.update(status="completed", ended_at=local_now(), updated_at=local_now())
            self.state["stage"] = "音频、转写与精修已归档"
            self.record.save_all()
            for path in [self.record.working_audio_path, *(self.record.directory / ".course-chunks").glob("*.pcm"), *(self.record.directory / ".course-chunks").glob("*.json")]:
                if path == self.record.audio_path:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # Final AAC is valid; a cleanup error must not undo completion.
        except (Exception, asyncio.CancelledError) as exc:
            self.state.update(stage="interrupted", error=str(exc) or "任务已停止，分段与转写已保留")
            self.record.metadata["status"] = "interrupted"
            self.record.save_all()
        finally:
            if pipeline:
                await pipeline.close()
            if inference and not inference.done():
                await asyncio.gather(inference, return_exceptions=True)
            if assembly and not assembly.done():
                await asyncio.gather(assembly, return_exceptions=True)
            if asr_owned:
                self.model.active_jobs = max(0, self.model.active_jobs - 1)
                self.model.schedule_unload()
            self.busy_ids.discard(self.record.id)

    async def stop(self, paused=False):
        # Idle jobs may hold metadata from before a note edit/AI job. Stopping
        # them must not write that stale snapshot back over the latest record.
        if self.task is None or self.task.done():
            return
        self.state["paused"] = paused
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        # run() (or its pre-start cleanup) persists the state before releasing
        # busy_ids; do not write again after another operation can acquire it.


class CourseStreamManager:
    def __init__(self, model, store, settings, busy_ids, is_record_busy=None):
        self.model, self.store, self.settings, self.busy_ids = model, store, settings, busy_ids
        self.jobs = {}
        self.is_record_busy = is_record_busy or (lambda ident: False)
        self.lock = asyncio.Lock()

    async def create(self, data):
        config = data.get("config") or {}
        try:
            required = ["sentence"]
            if config.get("ai_correction"):
                required.append("correction")
            if config.get("summary_enabled") or config.get("generate_summary", True):
                required.append("summary")
            # The SmartClass service owns these per-course operation switches.
            # Keep its explicit automatic-correction choice while still
            # rebuilding every provider and credential from saved settings.
            config = ai_preferences.task_config(
                config, required_modes=tuple(required),
                preserve_flags=("ai_correction", "summary_enabled", "translation"),
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        config["asr_params"] = normalize_asr_params(config.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"])
        key = str(data.get("key", ""))
        try:
            duration = float(data.get("duration", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "课次时长无效")
        if not key or len(key) > 100 or not 0 < duration <= 7200:
            raise HTTPException(400, "课次标识或时长无效")
        supplied = data.get("sources") or []
        if not isinstance(supplied, list) or len(supplied) > 500 or any(
            not isinstance(v, dict) or not v.get("video_id") or "segment_id" not in v for v in supplied
        ):
            raise HTTPException(400, "回放来源标识无效")
        sources = [{k: v[k] for k in ("video_id", "segment_id", "duration", "title", "start", "end") if k in v} for v in supplied]
        filename = "smartclass_" + key + ".flac"
        async with self.lock:
            record, extending = match_record(self.store.list_records(), key, sources)
            if extending:
                if record.id in self.busy_ids or self.is_record_busy(record.id):
                    raise HTTPException(409, "第一节仍在处理中，请完成后再补齐")
                self.busy_ids.add(record.id)
                preparation = asyncio.create_task(asyncio.to_thread(supplement, record, sources, key, float(data["duration"]), config.get("title", "课程回放")))
                try:
                    await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    await asyncio.gather(preparation, return_exceptions=True)
                    raise
                finally:
                    self.busy_ids.discard(record.id)
            if record and record.id in self.jobs and self.jobs[record.id].task is not None and not self.jobs[record.id].task.done():
                return self.jobs[record.id].snapshot()
            if record and record.metadata.get("status") == "completed":
                return {"id": record.id, "directory": str(record.directory), "status": "completed", "download_complete": True}
            if record and self.is_record_busy(record.id):
                raise HTTPException(409, "这条录音正在处理，请完成后再继续课程转写")
            if record and not record.metadata.get("course_stream"):
                raise HTTPException(409, "旧版导入请从语音工作台继续")
            if record is None:
                category = self.settings.add_category(config.get("category") or "未分类")
                starts = sorted(str(item.get("start")) for item in sources if item.get("start"))
                record = RecordingRecord.create_live(config.get("title") or "课程回放", category,
                    self.settings.storage_root, recorded_at=starts[0] if starts else None)
                record.metadata.update(kind="upload", status="processing", source_filename=filename,
                                       course_time=config.get('course_time') or {},
                                       course_stream={"chunks": [], "completed_chunks": 0, "expected_seconds": float(data["duration"]),
                                                      "sources": sources, "keys": [key], "automatic_title": config.get("title")},
                                       course_settings={"asr_prompt": config.get("context", ""), "terminology": config.get("terminology", []),
                                                        "asr_params": config["asr_params"], "ai_system_prompt": config.get("ai", {}).get("system_prompt", "")})
                self.store.paths[record.id] = record.directory
            worker = CourseStreamJob(record, config, self.model, self.busy_ids)
            self.jobs[record.id] = worker
            worker.start()
            return worker.snapshot()

    def get(self, ident):
        cached = self.jobs.get(ident)
        if cached is None or cached.task is None or cached.task.done():
            record = self.store.find(ident)
            if not record or not record.metadata.get("course_stream"):
                raise HTTPException(404, "找不到课程接收任务")
            # Readable after restart; only an explicit create starts processing.
            self.jobs[ident] = CourseStreamJob(record, record.metadata.get("batch_options", {}), self.model, self.busy_ids)
        return self.jobs[ident]

    async def close(self):
        await asyncio.gather(*(job.stop() for job in self.jobs.values()))


def course_stream_router(manager):
    router = APIRouter(prefix="/api/course-streams")

    def local(request):
        if request.headers.get("X-QwenASR-Client") != "smartclass":
            raise HTTPException(403, "请从本机课程回放页面操作")
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:8765", "http://localhost:8765"}:
            raise HTTPException(403, "来源不允许")

    @router.post("")
    async def create(request: Request):
        local(request)
        return await manager.create(await request.json())

    @router.get("/{ident}")
    async def status(ident: str):
        return manager.get(ident).snapshot()

    @router.put("/{ident}/chunks/{index}")
    async def chunk(ident: str, index: int, request: Request):
        local(request)
        if index < 0 or index > 500:
            raise HTTPException(400, "音频分段序号无效")
        body = bytearray()
        async for data in request.stream():
            body.extend(data)
            if len(body) > 16000 * 2 * 121:
                raise HTTPException(413, "音频分段过大")
        try:
            cursor = json.loads(request.headers.get("X-Course-Cursor", "null"))
        except ValueError:
            raise HTTPException(400, "音频断点无效")
        return await manager.get(ident).accept(index, bytes(body), cursor)

    @router.post("/{ident}/finish")
    async def finish(ident: str, request: Request):
        local(request)
        return await manager.get(ident).finish_input(int((await request.json()).get("count", -1)))

    @router.post("/{ident}/stop")
    async def stop(ident: str, request: Request):
        local(request)
        await manager.get(ident).stop(paused=bool((await request.body()) and (await request.json()).get("paused")))
        return manager.get(ident).snapshot()

    return router
