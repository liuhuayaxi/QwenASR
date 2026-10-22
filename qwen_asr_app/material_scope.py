"""Lesson-scoped course-material selection for review notes.

The old pipeline attached every active file of the course (all chapters) to
each lesson: every page that scored on any transcript block went into the
planning and writing payloads. This module picks the files that belong to
THIS lesson, using, in order of strength:

1. The matched 雨课堂 lesson title (e.g. "第四章 半导体激光器的性能 1") and the
   chapter number / 绪论 keyword in each file name ("第04章", "绪论").
2. Transcript keyword retrieval: each file's share of the page-match score
   over all transcript blocks (catches recaps of the previous chapter and
   lessons whose 雨课堂 title has no chapter, e.g. "9月5日授课").
3. Title bigram overlap between the lesson title and the file name.

With no chapter/keyword match it falls back to the top-k files by retrieval.
Every decision is recorded with its reasons so the job UI can show it.
"""
from __future__ import annotations

import re

VERSION = "lesson-scope-v1"
MAX_SOURCES = 4          # sensible cap on files per lesson
FALLBACK_TOP_K = 2       # retrieval-only fallback
RECAP_SHARE = 0.5        # extra file kept beside a chapter match when its retrieval share is this strong
FALLBACK_SHARE = 0.35    # fallback keeps files at least this close to the best one
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_GENERIC = re.compile(r"课件|翻转课堂|预习|思考题|答案解析|授课|讨论|更新版|视频|第|章|节|讲|[\d\s\-_()（）.、]+")


def _number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if not text or any(ch not in _CN for ch in text):
        return None
    if text == "十":
        return 10
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (_CN[tens] if tens else 1) * 10 + (_CN[ones] if ones else 0)
    return _CN[text]


def chapters(text: str) -> set[int]:
    found = set()
    for match in re.finditer(r"第\s*([0-9]+|[零一二两三四五六七八九十]+)\s*[章讲]", text or ""):
        value = _number(match.group(1))
        if value is not None:
            found.add(value)
    for match in re.finditer(r"(?i)\bch(?:apter)?\.?\s*0*([0-9]+)", text or ""):
        found.add(int(match.group(1)))
    return found


def _bigrams(text: str) -> set[str]:
    words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]{3,}", _GENERIC.sub(" ", text or "").lower())
    out = set()
    for word in words:
        out.update(word[i:i + 2] for i in range(len(word) - 1)) if re.search("[\u4e00-\u9fff]", word) else out.add(word)
    return out


def choose(sources: list[dict], retrieval: dict[str, float], lesson_title: str = "",
           selected: list | None = None) -> tuple[list[str], list[dict]]:
    """Return (chosen source ids in original order, per-file decisions).

    ``retrieval`` maps source_id to its summed transcript page-match score.
    """
    best = max(retrieval.values(), default=0) or 1
    lesson_chapters = chapters(lesson_title)
    intro = "绪论" in (lesson_title or "")
    title_terms = _bigrams(lesson_title)
    intro_derived = False
    if intro and not lesson_chapters and not any("绪论" in s.get("name", "") for s in sources):
        lesson_chapters = {1}   # 课程没有单独的绪论文件时，绪论在第 1 章课件里
        intro_derived = True
    rows = []
    for source in sources:
        name = source.get("name", "")
        share = round((retrieval.get(source["source_id"], 0) or 0) / best, 3)
        reasons = []
        matched = False
        file_chapters = chapters(name)
        if lesson_chapters and file_chapters & lesson_chapters:
            matched = True
            reasons.append(f"章节号与雨课堂课次一致（第{'、'.join(map(str, sorted(file_chapters & lesson_chapters)))}章）")
        if intro and "绪论" in name:
            matched = True
            reasons.append("雨课堂课次与文件名均为绪论")
        overlap = title_terms & _bigrams(name)
        title_score = round(len(overlap) / max(1, len(title_terms)), 3) if title_terms else 0
        if lesson_chapters and file_chapters and not matched:
            # A different chapter number outranks shared course vocabulary.
            title_score = 0
            reasons.append(f"文件属于第{'、'.join(map(str, sorted(file_chapters)))}章，与课次章节不同")
        if title_score >= 0.3:
            reasons.append(f"文件名与课次标题相近（{title_score:.0%}）")
        reasons.append(f"转写关键词检索占比 {share:.0%}")
        has_text = sum(len(str(p.get("text") or "").strip()) for p in source.get("pages") or []) >= 500
        rows.append({"source_id": source["source_id"], "name": name, "chapter_match": matched, "has_text": has_text,
                     "title_score": title_score, "retrieval_share": share, "reasons": reasons})
    if selected is not None:
        for row in rows:
            row["chosen"] = True
            row["reasons"].insert(0, "用户手动补选")
        return [row["source_id"] for row in rows], rows
    order = sorted(rows, key=lambda r: (not r["chapter_match"], -(r["title_score"] >= 0.3), -r["retrieval_share"]))
    if not any(r["retrieval_share"] for r in rows) and not any(r["chapter_match"] for r in rows):
        # No usable signal at all: keep the old whole-course behaviour.
        for row in rows:
            row.update(chosen=True, mode="no_signal_all")
            row["reasons"].append("没有课次章节或转写检索信号，沿用全部课件")
        return [r["source_id"] for r in rows], rows
    if any(r["chapter_match"] for r in rows):
        mode = "lesson_match"
        top = max(lesson_chapters, default=0)
        def keep(r):
            if r["chapter_match"] or r["title_score"] >= 0.3:
                return True
            later = [c for c in chapters(r["name"]) if c > top]
            if later and r["retrieval_share"] < 0.8:
                return False   # 后续章节：除非转写强烈指向，不读未来课次
            return r["retrieval_share"] >= RECAP_SHARE
        chosen = [r for r in order if keep(r)]
        if intro_derived and any(r["retrieval_share"] >= 0.8 and not r["chapter_match"] for r in chosen):
            # A "绪论" 雨课堂 title is often stale (the teacher reuses the first
            # lesson's activity). A
            # text-bearing file of the titled chapter that the transcript never
            # touches is dropped; scanned files without text stay.
            stale = [r for r in chosen if r["chapter_match"] and r["has_text"] and r["retrieval_share"] < 0.1]
            for r in stale:
                r["reasons"].append("课次标题章节与转写几乎不相关（标题可能沿用旧课次）")
            chosen = [r for r in chosen if r not in stale]
    else:
        mode = "retrieval_top_k"
        chosen = [r for r in order if r["retrieval_share"] >= FALLBACK_SHARE][:FALLBACK_TOP_K] or order[:1]
    qualified = {r["source_id"] for r in chosen}
    chosen = chosen[:MAX_SOURCES]
    ids = {r["source_id"] for r in chosen}
    for row in rows:
        row["chosen"] = row["source_id"] in ids
        row["mode"] = mode
        if not row["chosen"]:
            row["reasons"].append(f"超过每节最多 {MAX_SOURCES} 份的上限" if row["source_id"] in qualified
                                  else "与本节课次不符且检索占比不足")
    return [r["source_id"] for r in rows if r["chosen"]], rows


def summary(rows: list[dict], lesson_title: str = "") -> str:
    chosen = [r["name"] for r in rows if r.get("chosen")]
    head = f"按雨课堂课次「{lesson_title}」" if lesson_title else "按转写检索"
    return f"{head}选用 {len(chosen)}/{len(rows)} 份课件：" + "、".join(chosen)


def lesson_title_for(record) -> str:
    """The matched 雨课堂 lesson title, or "" (never blocks generation)."""
    try:
        from .yuketang.retrieval import forced_for_record
        from .yuketang.store import BankStore
        lesson, _ = forced_for_record(record, BankStore(), record.metadata.get("category"))
        return str((lesson or {}).get("title") or "")
    except Exception:
        return ""
