from __future__ import annotations

import re
from typing import Any

import numpy as np

from .settings import SAMPLE_RATE


def has_repetitive_tail(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 500:
        return False
    return bool(re.search(r"(.{8,80})\1{4,}", compact[-2_000:]))


def trim_repetitive_tail(text: str) -> str:
    """Keep one copy of a phrase when decoding loops on the same filler."""
    match = re.search(r"(.{1,32}?)(?:\1){5,}", text or "", flags=re.S)
    if not match:
        return (text or "").strip()
    return ((text or "")[: match.start()] + match.group(1)).strip()


def bisect_audio_range(
    audio: np.ndarray, start: int, end: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    midpoint = (start + end) // 2
    frame = int(0.10 * SAMPLE_RATE)
    low = max(start + int(10 * SAMPLE_RATE), midpoint - int(8 * SAMPLE_RATE))
    high = min(end - int(10 * SAMPLE_RATE), midpoint + int(8 * SAMPLE_RATE))
    if high > low + frame:
        section = audio[low : high - ((high - low) % frame)]
        if len(section) >= frame:
            energies = np.mean(
                np.square(section.reshape(-1, frame)), axis=1, dtype=np.float64
            )
            midpoint = low + (int(np.argmin(energies)) + 1) * frame
    return (start, midpoint), (midpoint, end)


def pending_ai_groups(
    segments: list[dict[str, Any]],
    existing_groups: list[dict[str, Any]],
    max_chars: int = 6_000,
) -> list[list[dict[str, Any]]]:
    completed_ids = {
        int(segment_id)
        for group in existing_groups
        if isinstance(group, dict) and not group.get("ai_error")
        for segment_id in group.get("segment_ids", [])
    }
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for segment in segments:
        if int(segment.get("id", 0)) in completed_ids or not str(segment.get("raw") or "").strip():
            continue
        text_length = len(str(segment.get("raw") or ""))
        if current and current_chars + text_length > max_chars:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += text_length
    if current:
        groups.append(current)
    return groups


def due_review_windows(
    segments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    interval_seconds: int = 300,
) -> list[tuple[int, int, list[dict[str, Any]]]]:
    """Return unreviewed, complete timeline windows for periodic AI cleanup."""
    completed_until = max((float(item.get("end", 0)) for item in segments), default=0.0)
    reviewed_ends = {
        int(round(float(item.get("window_end", item.get("end", 0)))))
        for item in reviews
        if isinstance(item, dict)
    }
    windows = []
    last_complete = int(completed_until // interval_seconds) * interval_seconds
    for window_end in range(interval_seconds, last_complete + 1, interval_seconds):
        if window_end in reviewed_ends:
            continue
        window_start = window_end - interval_seconds
        members = [
            item
            for item in segments
            if window_start < float(item.get("end", 0)) <= window_end
            and str(item.get("raw") or "").strip()
        ]
        windows.append((window_start, window_end, members))
    return windows


def due_summary_windows(
    segments: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    interval_seconds: int,
) -> list[tuple[int, int, list[dict[str, Any]]]]:
    """Return complete, not-yet-summarized timeline windows."""
    interval = max(60, int(interval_seconds))
    completed_until = max((float(item.get("end", 0)) for item in segments), default=0.0)
    summarized_ends = {
        int(round(float(item.get("end", 0))))
        for item in summaries
        if isinstance(item, dict)
    }
    windows = []
    last_complete = int(completed_until // interval) * interval
    for window_end in range(interval, last_complete + 1, interval):
        if window_end in summarized_ends:
            continue
        window_start = window_end - interval
        members = [
            item
            for item in segments
            if window_start < float(item.get("end", 0)) <= window_end
            and str(item.get("corrected") or item.get("raw") or "").strip()
        ]
        windows.append((window_start, window_end, members))
    return windows
