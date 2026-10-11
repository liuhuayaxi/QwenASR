"""One timestamped source for reading, detailed notes, exports and IdeaShell."""
from __future__ import annotations

import hashlib
import re
from typing import Any


def clock(value: float) -> str:
    seconds = max(0, int(float(value)))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def transcript_blocks(record: Any) -> list[dict[str, Any]]:
    segments = sorted(record.segments, key=lambda s: (float(s.get("start", 0)), int(s.get("id", 0))))
    by_id = {int(s["id"]): s for s in segments if s.get("id") is not None}
    covered: set[int] = set()
    blocks: list[dict[str, Any]] = []
    global_result = record.metadata.get("ai_global_rewrite") or {}
    candidates = []
    if global_result.get("complete"):
        candidates.extend(global_result.get("parts") or [])
    candidates.extend(record.metadata.get("ai_reviews") or [])
    for part in candidates:
        ids = [int(value) for value in part.get("segment_ids", [])]
        text = str(part.get("corrected") or "").strip()
        if not ids or not text or part.get("ai_error") or covered.intersection(ids) or any(i not in by_id for i in ids):
            continue
        members = [by_id[i] for i in ids]
        blocks.append({"start": min(float(s.get("start", 0)) for s in members),
                       "end": max(float(s.get("end", 0)) for s in members),
                       "text": text, "refined": True, "segment_ids": ids})
        covered.update(ids)
    for segment in segments:
        if int(segment.get("id", 0)) in covered:
            continue
        if segment.get("ai_suppressed"):
            continue
        text = str(segment.get("corrected") or segment.get("raw") or "").strip()
        if text:
            blocks.append({"start": float(segment.get("start", 0)), "end": float(segment.get("end", 0)),
                           "text": text, "refined": bool(segment.get("corrected")) and not segment.get("ai_error"),
                           "segment_ids": [int(segment.get("id", 0))]})
    return sorted(blocks, key=lambda block: block["start"])


def transcript_markdown(record: Any) -> tuple[str, int]:
    blocks = transcript_blocks(record)
    missing = sum(not block["refined"] for block in blocks)
    text = "\n\n".join(
        f"### {clock(block['start'])}–{clock(block['end'])}"
        + (" · 未精修" if not block["refined"] else "") + f"\n\n{block['text']}"
        for block in blocks
    )
    return text, missing


def compose_push(record: Any, mode: str = "combined") -> dict[str, Any]:
    if mode not in {"combined", "summary", "transcript"}:
        raise ValueError("推送内容必须选择总结＋精修原文、仅总结或仅精修原文")
    summary = str((record.metadata.get("ai_note_summary") or {}).get("summary") or "").strip()
    transcript, missing = transcript_markdown(record)
    if mode != "transcript" and not summary:
        raise ValueError("请先生成并保存 AI 总结，或选择仅推送精修原文")
    if mode != "summary" and not transcript:
        raise ValueError("这条录音没有可以推送的转写内容")
    title = str(record.metadata.get("title") or "未命名录音")
    # Visible section boundaries survive Markdown sanitizers at the destination.
    body = [f"# {title}", f"课程：{record.metadata.get('category') or '未分类'}  \n"
            f"日期：{record.metadata.get('started_at') or '未知'}  \n时长：{clock(record.duration)}"]
    if mode != "transcript":
        # Generated summaries already contain a title and metadata. Keep the
        # original verbatim inside its section to make pull round trips lossless.
        body.extend(["## AI 总结", summary])
    if mode != "summary":
        body.extend(["---", "## 精修录音原文（含时间戳）"])
        if missing:
            body.append(f"> {missing} 个片段尚未完成精修，已标注“未精修”并保留原始识别内容。")
        body.append(transcript)
    markdown = "\n\n".join(body).strip() + "\n"
    return {"content": markdown, "mode": mode, "unrefined_blocks": missing if mode != "summary" else 0,
            "digest": hashlib.sha256(markdown.encode()).hexdigest(), "characters": len(markdown)}


def compose_push_part(
    record: Any,
    mode: str,
    start_seconds: float,
    end_seconds: float,
    part_index: int,
    part_count: int = 2,
) -> dict[str, Any]:
    """Compose one time-windowed note for a split IdeaShell recording."""
    if mode not in {"combined", "summary", "transcript"}:
        raise ValueError("推送内容必须选择总结＋精修原文、仅总结或仅精修原文")
    summary = str((record.metadata.get("ai_note_summary") or {}).get("summary") or "").strip()
    blocks = [block for block in transcript_blocks(record)
              if float(block["end"]) > float(start_seconds)
              and float(block["start"]) < float(end_seconds)]
    missing = sum(not block["refined"] for block in blocks)
    transcript = "\n\n".join(
        f"### {clock(block['start'])}–{clock(block['end'])}"
        + (" · 未精修" if not block["refined"] else "") + f"\n\n{block['text']}"
        for block in blocks
    )
    if mode != "transcript" and not summary:
        raise ValueError("请先生成并保存 AI 总结，或选择仅推送精修原文")
    if mode != "summary" and not transcript:
        raise ValueError("这一段没有可以推送的转写内容")
    title = str(record.metadata.get("title") or "未命名录音")
    label = "上半段" if part_index == 1 else "下半段"
    body = [f"# {title}（{label}）", f"课程：{record.metadata.get('category') or '未分类'}  \n"
            f"日期：{record.metadata.get('started_at') or '未知'}  \n时长：{clock(max(0, end_seconds - start_seconds))}"]
    if mode != "transcript":
        body.extend(["## AI 总结", summary])
    if mode != "summary":
        body.extend(["---", f"## 精修录音原文（{label}，含时间戳）"])
        if missing:
            body.append(f"> {missing} 个片段尚未完成精修，已标注“未精修”并保留原始识别内容。")
        body.append(transcript)
    markdown = "\n\n".join(body).strip() + "\n"
    return {"content": markdown, "mode": mode, "part_index": part_index,
            "part_count": part_count, "title": f"{title}（{label}）",
            "unrefined_blocks": missing if mode != "summary" else 0,
            "digest": hashlib.sha256(markdown.encode()).hexdigest(),
            "characters": len(markdown)}


def summary_from_archive(markdown: str, sync: dict[str, Any]) -> str:
    if not sync.get("content_mode"):
        return markdown  # Legacy summary-only notes have no wrapper.
    start = re.search(r"^## AI 总结\s*$", markdown, re.M)
    if not start:
        raise ValueError("远端笔记没有可识别的“AI 总结”章节；本地总结未被覆盖")
    tail = markdown[start.end():]
    boundary = re.search(r"^## 精修录音原文（含时间戳）\s*$", tail, re.M)
    if boundary:
        tail = tail[:boundary.start()]
        tail = re.sub(r"\n---\s*$", "", tail.rstrip())
    result = tail.strip()
    if not result:
        raise ValueError("远端 AI 总结为空；本地总结未被覆盖")
    return result
