from __future__ import annotations

import asyncio
import json
import wave
from collections import deque
from typing import Any

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from .ai import (
    context_window_for,
    polish_and_translate,
    polish_timestamped_segments_resilient,
    retry_transient_ai,
    summarize_transcript,
)
from .context_budget import chunk_text_blocks
from .model_runtime import ModelManager
from .review import due_review_windows, due_summary_windows
from .settings import (
    DEFAULT_AI_SYSTEM_PROMPT,
    DEFAULT_ASR_PARAMS,
    DEFAULT_LIVE_PARAMS,
    SAMPLE_RATE,
    normalize_asr_params,
    normalize_live_params,
)
from .storage import RecordingRecord, RecordingStore, format_clock
from .terminology import asr_params_with_terminology, asr_terminology_context, normalize_terminology


class TranscriptionSession:
    def __init__(
        self,
        websocket: WebSocket,
        model_manager: ModelManager,
        recording_store: RecordingStore,
    ) -> None:
        self.ws = websocket
        self.model_manager = model_manager
        self.recording_store = recording_store
        self.config: dict[str, Any] = {
            "language": None,
            "context": "",
            "ai_correction": False,
            "summary_enabled": False,
            "summary_interval_minutes": 10,
            "translation": "off",
            "vad_threshold": 0.008,
            **DEFAULT_LIVE_PARAMS,
        }
        self.pending_config: dict[str, Any] = {}
        self.preroll: deque[np.ndarray] = deque()
        self.preroll_samples = 0
        self.chunks: list[np.ndarray] = []
        self.sample_count = 0
        self.silence_samples = 0
        self.speech_hits = 0
        self.voiced_samples = 0
        self.speaking = False
        self.segment_id = 1
        self.last_partial_at = 0
        self.partial_running = False
        self.tasks: set[asyncio.Task[Any]] = set()
        self.review_tasks: set[asyncio.Task[Any]] = set()
        self.periodic_tasks: dict[str, asyncio.Task[Any]] = {}
        self.periodic_dirty: dict[str, bool] = {"review": False, "summary": False}
        self.review_lock = asyncio.Lock()
        self.summary_lock = asyncio.Lock()
        self.recording: RecordingRecord | None = None
        self.accepting_audio = False
        self.recovered = False

    async def send(self, payload: dict[str, Any]) -> None:
        try:
            await self.ws.send_json(payload)
        except (RuntimeError, OSError, WebSocketDisconnect):
            pass

    def track(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def track_review(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.review_tasks.add(task)
        task.add_done_callback(self.review_tasks.discard)

    def schedule_periodic(self, kind: str, recording: RecordingRecord, config: dict[str, Any]) -> None:
        """Coalesce many completed ASR segments into one periodic worker."""
        self.periodic_dirty[kind] = True
        current = self.periodic_tasks.get(kind)
        if current is not None and not current.done():
            return

        async def run() -> None:
            try:
                while self.periodic_dirty[kind]:
                    self.periodic_dirty[kind] = False
                    if kind == "review":
                        await self.run_periodic_reviews(recording, config)
                    else:
                        await self.run_periodic_summaries(recording, config)
            finally:
                self.periodic_tasks.pop(kind, None)

        task = asyncio.create_task(run())
        self.periodic_tasks[kind] = task
        self.review_tasks.add(task)
        task.add_done_callback(self.review_tasks.discard)

    def update_config(self, values: dict[str, Any]) -> bool:
        """Apply settings now, or queue them until the current utterance ends."""
        clean = {key: value for key, value in values.items() if key != "type"}
        if self.speaking and self.chunks:
            self.pending_config.update(clean)
            return True
        self.config.update(clean)
        return False

    def apply_pending_config(self) -> None:
        if self.pending_config:
            self.config.update(self.pending_config)
            self.pending_config.clear()

    def add_preroll(self, chunk: np.ndarray) -> None:
        self.preroll.append(chunk)
        self.preroll_samples += len(chunk)
        limit = int(0.45 * SAMPLE_RATE)
        while self.preroll and self.preroll_samples - len(self.preroll[0]) >= limit:
            self.preroll_samples -= len(self.preroll.popleft())

    async def ingest(self, data: bytes) -> None:
        if not self.accepting_audio or self.recording is None or self.recording.metadata.get("status") != "active":
            return
        usable = len(data) - (len(data) % 4)
        if usable <= 0:
            return
        audio = np.frombuffer(data[:usable], dtype="<f4").copy()
        if not len(audio):
            return
        self.recording.append_audio(audio)
        rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
        threshold = max(0.002, min(float(self.config.get("vad_threshold", 0.008)), 0.08))

        if not self.speaking:
            self.add_preroll(audio)
            self.speech_hits = self.speech_hits + 1 if rms >= threshold else 0
            if self.speech_hits >= 2:
                self.speaking = True
                self.chunks = list(self.preroll)
                self.sample_count = sum(len(c) for c in self.chunks)
                self.preroll.clear()
                self.preroll_samples = 0
                self.silence_samples = 0
                self.voiced_samples = self.speech_hits * len(audio)
                self.last_partial_at = self.sample_count
                await self.send({"type": "speech", "active": True, "id": self.segment_id})
            return

        self.chunks.append(audio)
        self.sample_count += len(audio)
        if rms < threshold * 0.62:
            self.silence_samples += len(audio)
        else:
            self.silence_samples = 0
            self.voiced_samples += len(audio)

        if self.sample_count >= int(1.5 * SAMPLE_RATE) and (
            # BF16 1.7B is fast on Apple Silicon, but repeated full-prefix decoding
            # is the main energy cost. A configurable ~6 s cadence stays interactive without
            # keeping the GPU busy continuously.
            self.sample_count - self.last_partial_at
            >= int(
                max(2.0, min(float(self.config.get("partial_interval_seconds", 6.0)), 15.0))
                * SAMPLE_RATE
            )
        ):
            self.last_partial_at = self.sample_count
            if not self.partial_running:
                snapshot = np.concatenate(self.chunks)
                current_id = self.segment_id
                self.partial_running = True
                self.track(self.run_partial(snapshot, current_id))

        silence_finalize = max(
            0.5, min(float(self.config.get("silence_finalize_seconds", 1.5)), 4.0)
        )
        min_voiced = max(0.2, min(float(self.config.get("min_voiced_seconds", 0.55)), 2.0))
        max_segment = max(15.0, min(float(self.config.get("max_segment_seconds", 45.0)), 90.0))
        if self.silence_samples >= int(silence_finalize * SAMPLE_RATE):
            if self.voiced_samples < int(min_voiced * SAMPLE_RATE):
                self.reset_audio()
                await self.send({"type": "speech", "active": False, "id": self.segment_id})
            else:
                await self.detach_and_process("silence")
        elif self.sample_count >= int(max_segment * SAMPLE_RATE):
            await self.detach_and_process("length", continue_speaking=True)

    async def run_partial(self, audio: np.ndarray, segment_id: int) -> None:
        try:
            text, language, seconds = await self.model_manager.transcribe(
                audio,
                self.config.get("language"),
                asr_terminology_context(
                    str(self.config.get("context", "")), self.config.get("terminology")
                ),
                priority=2,
                is_current=lambda: segment_id == self.segment_id and self.speaking,
                asr_params=asr_params_with_terminology(
                    self.config.get("asr_params"), self.config.get("terminology")
                ),
            )
            if segment_id == self.segment_id and self.speaking and text:
                await self.send(
                    {
                        "type": "partial",
                        "id": segment_id,
                        "raw": text,
                        "language": language,
                        "latency": seconds,
                    }
                )
        except Exception as exc:
            await self.send({"type": "error", "message": f"滚动转写失败：{exc}"})
        finally:
            self.partial_running = False

    async def detach_and_process(self, reason: str, continue_speaking: bool = False) -> None:
        if not self.chunks or self.sample_count < int(0.35 * SAMPLE_RATE):
            self.reset_audio()
            return
        recording = self.recording
        if recording is None:
            self.reset_audio()
            return
        audio = np.concatenate(self.chunks)
        current_id = recording.reserve_segment_id()
        segment_end = recording.duration
        segment_start = max(0.0, segment_end - len(audio) / SAMPLE_RATE)
        recording.metadata.setdefault("pending_segments", {})[str(current_id)] = {
            "id": current_id, "start": segment_start, "end": segment_end,
        }
        recording.save_metadata()
        config_snapshot = json.loads(json.dumps(self.config))
        self.segment_id = recording.next_segment_id
        self.reset_audio(continue_speaking=continue_speaking)
        self.apply_pending_config()
        await self.send({"type": "speech", "active": continue_speaking, "id": self.segment_id})
        await self.send({"type": "processing", "id": current_id})
        self.track(
            self.run_final(
                audio,
                current_id,
                config_snapshot,
                reason,
                recording,
                segment_start,
                segment_end,
            )
        )

    def reset_audio(self, continue_speaking: bool = False) -> None:
        self.chunks = []
        self.sample_count = 0
        self.silence_samples = 0
        self.speech_hits = 0
        self.voiced_samples = 0
        self.preroll.clear()
        self.preroll_samples = 0
        self.speaking = continue_speaking
        self.last_partial_at = 0

    async def run_final(
        self,
        audio: np.ndarray,
        segment_id: int,
        config: dict[str, Any],
        reason: str,
        recording: RecordingRecord,
        segment_start: float,
        segment_end: float,
    ) -> None:
        try:
            metrics = {}
            raw, language, seconds = await self.model_manager.transcribe(
                audio,
                config.get("language"),
                asr_terminology_context(
                    str(config.get("context", "")), config.get("terminology")
                ),
                priority=0, metrics=metrics,
                asr_params=asr_params_with_terminology(
                    config.get("asr_params"), config.get("terminology")
                ),
            )
            recording.metadata["transcribed_until_sample"] = max(
                int(recording.metadata.get("transcribed_until_sample", 0)), round(segment_end * SAMPLE_RATE))
            if not raw:
                recording.metadata.setdefault("pending_segments", {}).pop(str(segment_id), None)
                recording.save_metadata()
                await self.send({"type": "discard", "id": segment_id})
                return
            segment: dict[str, Any] = {
                "id": segment_id,
                "raw": raw,
                "language": language,
                "latency": seconds,
                "metrics": metrics,
                "duration": round(len(audio) / SAMPLE_RATE, 1),
                "start": round(segment_start, 2),
                "end": round(segment_end, 2),
                "reason": reason,
                "ai_pending": bool(config.get("ai_correction"))
                or config.get("translation", "off") != "off",
            }
            if segment["ai_pending"]:
                segment["ai_requested"] = {
                    "correction": bool(config.get("ai_correction")),
                    "translation": str(config.get("translation", "off")),
                }
            recording.metadata.setdefault("pending_segments", {}).pop(str(segment_id), None)
            recording.upsert_segment(segment)
            await self.send({"type": "final", **segment})
            if config.get("ai_correction") or config.get("translation", "off") != "off":
                try:
                    ai_config = json.loads(json.dumps(config))
                    ai_options = ai_config.get("ai") if isinstance(ai_config.get("ai"), dict) else {}
                    try:
                        history_chars = int(ai_options.get("history_chars", 2000))
                    except (TypeError, ValueError):
                        history_chars = 2000
                    history_chars = max(0, min(history_chars, 12_000))
                    previous_parts: list[str] = []
                    for saved in recording.segments:
                        if int(saved.get("id", 0)) >= segment_id:
                            continue
                        saved_text = str(saved.get("corrected") or saved.get("raw") or "").strip()
                        if saved_text:
                            previous_parts.append(saved_text)
                    if history_chars:
                        ai_config["_transcript_context"] = "\n".join(previous_parts)[-history_chars:]
                    enrichment = await retry_transient_ai(
                        lambda: polish_and_translate(raw, language, ai_config)
                    )
                    segment.update(enrichment)
                    segment["ai_pending"] = False
                    recording.upsert_segment(segment)
                    await self.send({"type": "ai_result", "id": segment_id, **enrichment})
                except Exception as exc:
                    segment["ai_pending"] = False
                    segment["ai_error"] = str(exc)
                    recording.upsert_segment(segment)
                    await self.send({"type": "ai_error", "id": segment_id, "message": str(exc)})
            if config.get("ai_correction"):
                self.schedule_periodic("review", recording, config)
            if config.get("summary_enabled"):
                self.schedule_periodic("summary", recording, config)
        except Exception as exc:
            await self.send({"type": "error", "id": segment_id, "message": f"转写失败：{exc}"})

    async def run_periodic_reviews(
        self, recording: RecordingRecord, config: dict[str, Any]
    ) -> None:
        async with self.review_lock:
            reviews = recording.metadata.get("ai_reviews")
            if not isinstance(reviews, list):
                reviews = []
            for window_start, window_end, window_segments in due_review_windows(
                recording.segments, reviews
            ):
                if not window_segments:
                    reviews.append(
                        {
                            "id": f"{window_start}-{window_end}",
                            "start": window_start,
                            "end": window_end,
                            "window_start": window_start,
                            "window_end": window_end,
                            "segment_ids": [],
                            "raw": "",
                            "corrected": "",
                        }
                    )
                    recording.metadata["ai_reviews"] = reviews
                    recording.save_all()
                    continue

                raw_block = "\n\n".join(
                    f"[{format_clock(float(segment.get('start', 0)))}–"
                    f"{format_clock(float(segment.get('end', 0)))}]\n"
                    f"{segment.get('corrected') or segment.get('raw') or ''}"
                    for segment in window_segments
                )
                review_config = json.loads(json.dumps(config))
                review_config["_review_mode"] = True
                previous_text = "\n".join(
                    str(segment.get("corrected") or segment.get("raw") or "")
                    for segment in recording.segments
                    if float(segment.get("end", 0)) <= window_start
                )
                ai_options = (
                    review_config.get("ai")
                    if isinstance(review_config.get("ai"), dict)
                    else {}
                )
                try:
                    history_chars = max(
                        0, min(int(ai_options.get("history_chars", 2000)), 12_000)
                    )
                except (TypeError, ValueError):
                    history_chars = 2000
                if history_chars:
                    review_config["_transcript_context"] = previous_text[-history_chars:]

                await self.send(
                    {
                        "type": "ai_review_pending",
                        "start": window_start,
                        "end": window_end,
                        "segment_ids": [int(segment["id"]) for segment in window_segments],
                    }
                )
                review: dict[str, Any] = {
                    "id": f"{window_start}-{window_end}",
                    "start": window_start,
                    "end": window_end,
                    "window_start": window_start,
                    "window_end": window_end,
                    "segment_ids": [int(segment["id"]) for segment in window_segments],
                    "raw": raw_block,
                }
                try:
                    mapped_blocks = await retry_transient_ai(
                        lambda: polish_timestamped_segments_resilient(
                            window_segments,
                            str(window_segments[0].get("language") or ""),
                            review_config,
                            mode="review",
                        )
                    )
                    by_id = {int(segment["id"]): segment for segment in window_segments}
                    window_reviews: list[dict[str, Any]] = []
                    for index, mapped in enumerate(mapped_blocks):
                        members = [by_id[value] for value in mapped["segment_ids"]]
                        item = {
                            "id": f"{window_start}-{window_end}-{index + 1}",
                            "start": min(float(member.get("start", 0)) for member in members),
                            "end": max(float(member.get("end", 0)) for member in members),
                            "window_start": window_start,
                            "window_end": window_end,
                            "segment_ids": mapped["segment_ids"],
                            "raw": "\n\n".join(str(member.get("corrected") or member.get("raw") or "") for member in members),
                            "corrected": mapped["corrected"],
                        }
                        window_reviews.append(item)
                    reviews.extend(window_reviews)
                except Exception as exc:
                    review["ai_error"] = str(exc)
                    review["error_kind"] = getattr(exc, "kind", "upstream")
                    reviews.append(review)
                recording.metadata["ai_reviews"] = reviews
                recording.metadata["last_ai_review_at"] = window_end
                recording.save_all()
                if review.get("ai_error"):
                    await self.send({"type": "ai_review", "review": review})
                else:
                    for item in window_reviews:
                        await self.send({"type": "ai_review", "review": item})

    async def run_periodic_summaries(
        self, recording: RecordingRecord, config: dict[str, Any]
    ) -> None:
        async with self.summary_lock:
            config = {**config, "_live_summary": True}
            try:
                interval_minutes = max(
                    1, min(int(config.get("summary_interval_minutes", 10)), 120)
                )
            except (TypeError, ValueError):
                interval_minutes = 10
            summaries = recording.metadata.get("ai_summaries")
            if not isinstance(summaries, list):
                summaries = []
            previous_summary = ""
            for saved in summaries:
                if isinstance(saved, dict) and saved.get("summary") and not saved.get("ai_error"):
                    previous_summary = str(saved["summary"])
            if any(isinstance(saved, dict) and saved.get("ai_error") for saved in summaries):
                # A cumulative chain cannot safely jump over a failed window.
                # Keep new windows pending until the user resumes from the
                # earliest failure in the task center.
                return

            for window_start, window_end, window_segments in due_summary_windows(
                recording.segments, summaries, interval_minutes * 60
            ):
                summary_item: dict[str, Any] = {
                    "id": f"{window_start}-{window_end}",
                    "start": window_start,
                    "end": window_end,
                    "segment_ids": [int(segment["id"]) for segment in window_segments],
                }
                if not window_segments:
                    summary_item["summary"] = previous_summary
                    summary_item["key_points"] = []
                    summaries.append(summary_item)
                    recording.metadata["ai_summaries"] = summaries
                    recording.save_all()
                    continue

                blocks = [
                    f"[{format_clock(float(segment.get('start', 0)))}–"
                    f"{format_clock(float(segment.get('end', 0)))}]\n"
                    f"{segment.get('corrected') or segment.get('raw') or ''}"
                    for segment in window_segments
                ]
                chunks = chunk_text_blocks(blocks, context_window_for(config, "review"))
                await self.send(
                    {
                        "type": "ai_summary_pending",
                        "start": window_start,
                        "end": window_end,
                        "parts": len(chunks),
                    }
                )
                try:
                    key_points: list[str] = []
                    for chunk in chunks:
                        result = await retry_transient_ai(
                            lambda: summarize_transcript(
                                chunk, previous_summary, config, provider_mode="review"
                            )
                        )
                        previous_summary = str(result["summary"])
                        key_points = list(result.get("key_points") or [])
                    summary_item.update(
                        {
                            "summary": previous_summary,
                            "key_points": key_points,
                            "parts": len(chunks),
                        }
                    )
                except Exception as exc:
                    summary_item["ai_error"] = str(exc)
                    summary_item["error_kind"] = getattr(exc, "kind", "upstream")
                # Keep one authoritative attempt per time window. Historical
                # duplicate rows are ignored by replacing the latest match.
                summaries = [
                    item for item in summaries
                    if not isinstance(item, dict) or int(round(float(item.get("end", -1)))) != window_end
                ]
                summaries.append(summary_item)
                recording.metadata["ai_summaries"] = summaries
                recording.metadata["last_ai_summary_at"] = window_end
                recording.save_all()
                await self.send({"type": "ai_summary", "summary": summary_item})
                if summary_item.get("ai_error"):
                    # A cumulative chain must resume from its earliest failure.
                    # Later windows are left pending for an explicit retry.
                    break

    async def start_or_resume(self) -> None:
        if self.recording and (self.recording.metadata.get("finalization") or {}).get("status") == "running":
            raise ValueError("录音正在结束保存，请等待完成后新建录音")
        self.config["asr_params"] = normalize_asr_params(
            self.config.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"]
        )
        self.config["terminology"] = normalize_terminology(self.config.get("terminology"))
        recording, created = self.recording_store.start_or_resume(
            str(self.config.get("title") or ""),
            str(self.config.get("category") or "未分类"),
        )
        if created:
            ai_options = self.config.get("ai") if isinstance(self.config.get("ai"), dict) else {}
            recording.metadata["course_settings"] = {
                "asr_prompt": str(self.config.get("context") or ""),
                "terminology": list(self.config.get("terminology") or []),
                "asr_params": normalize_asr_params(
                    asr_params_with_terminology(
                        self.config.get("asr_params"), self.config.get("terminology")
                    ),
                    DEFAULT_ASR_PARAMS["max_tokens"],
                ),
                "live_params": normalize_live_params(
                    {key: self.config.get(key, value) for key, value in DEFAULT_LIVE_PARAMS.items()}
                ),
                "ai_system_prompt": str(ai_options.get("system_prompt") or DEFAULT_AI_SYSTEM_PROMPT),
            }
            recording.save_metadata()
        self.recording = recording
        await self.recover_pending()
        self.accepting_audio = True
        self.segment_id = recording.next_segment_id
        await self.send(
            {
                "type": "session_started",
                "created": created,
                "session": recording.public(),
            }
        )
        self.track(self.model_manager.load())
        if self.config.get("ai_correction"):
            self.schedule_periodic("review", recording, json.loads(json.dumps(self.config)))
        if self.config.get("summary_enabled"):
            self.schedule_periodic("summary", recording, json.loads(json.dumps(self.config)))

    async def recover_pending(self) -> None:
        """Recover audio accepted before a server restart but not yet transcribed."""
        if self.recovered or self.recording is None:
            return
        self.recovered = True
        record = self.recording
        path = record.working_audio_path
        if not path.exists() or path.suffix.lower() != ".wav":
            return
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
        record.metadata["sample_count"] = max(frames, int(record.metadata.get("sample_count", 0)))
        pending = record.metadata.setdefault("pending_segments", {})
        covered_end = max([float(s.get("end", 0)) for s in record.segments]
                          + [float(s.get("end", 0)) for s in pending.values()]
                          + [int(record.metadata.get("transcribed_until_sample", 0)) / SAMPLE_RATE])
        cursor = min(frames, round(covered_end * SAMPLE_RATE))
        while cursor < frames:
            end = min(frames, cursor + 45 * SAMPLE_RATE)
            segment_id = record.reserve_segment_id()
            pending[str(segment_id)] = {"id": segment_id, "start": cursor / SAMPLE_RATE, "end": end / SAMPLE_RATE}
            cursor = end
        record.save_metadata()
        for item in list(pending.values()):
            def read_range(item=item):
                with wave.open(str(path), "rb") as audio:
                    audio.setpos(min(frames, round(item["start"] * SAMPLE_RATE)))
                    return np.frombuffer(audio.readframes(round((item["end"] - item["start"]) * SAMPLE_RATE)),
                                         dtype="<i2").astype(np.float32) / 32768.0
            samples = await asyncio.to_thread(read_range)
            self.track(self.run_final(samples, int(item["id"]), json.loads(json.dumps(self.config)),
                                      "recovery", record, float(item["start"]), float(item["end"])))

    async def pause(self, send_status: bool = True) -> None:
        # Stop capture immediately. A slow ASR/AI request must not hold the
        # pause acknowledgement or prevent the user from resuming capture.
        self.accepting_audio = False
        if self.speaking and self.chunks:
            await self.detach_and_process("pause")
        else:
            self.reset_audio()
        if self.recording is not None:
            self.recording.save_metadata()
        if send_status:
            await self.send({"type": "paused", "processing": bool(self.tasks or self.review_tasks),
                             "session": self.recording.public() if self.recording else None})

    async def drain(self) -> None:
        while self.tasks:
            pending = list(self.tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            self.tasks.difference_update(pending)
        # A five-minute review can be launched by the final sentence task above.
        # Wait for it before persisting/ending so the saved notes cannot miss a
        # review that has already been accepted by the AI service.
        while self.review_tasks:
            pending = list(self.review_tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            self.review_tasks.difference_update(pending)
        if self.recording is not None:
            self.recording.save_all()

    async def end(self) -> None:
        await self.pause(send_status=False)
        await self.drain()
        record = self.recording or self.recording_store.active
        if record is not None:
            await asyncio.to_thread(record.complete)
            if self.recording_store.active is record:
                self.recording_store.active = None
        await self.send({"type": "ended", "session": record.public() if record else None})
        self.recording = None
