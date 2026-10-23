"""Embed the original 雨课堂 question images in a note's 课上习题 section.

A question whose prompt (or teacher explanation) lives in an image is shown
with that image instead of a written description of it:

* stem image — the Body image of a quiz item (genuine stem text kept), or
  the problem slide when the question has no real text stem, is subjective,
  or its slide carries a figure (vision text mentions 附图); a slide already
  shows the whole prompt, so its text and option lines are not repeated;
* explanation images — the teacher's 解析 pictures (remarkRich shapes),
  skipping blank title strips (white text on transparency).

Images are written once per note package as ``assets/fig-<16 hex>.png``
(the name the note-asset route, 闪念贝壳 push and sharingnotes export accept)
and registered in ``yuketang-images.json`` so later image binding keeps them.
The student's own uploaded answer pictures are never embedded.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

from ..durable import atomic_bytes, atomic_json
from .store import BankStore

REGISTRY = "yuketang-images.json"
_IMAGE_LINE = re.compile(r"^!\[[^\]]*\]\((assets/fig-[a-f0-9]{16}\.png)\)\s*$")
_OPTION = re.compile(r"^\s*-\s+[A-H]\.\s")


def _genuine(text: Any) -> str:
    value = re.sub(r"\[图片\]", "", str(text or "")).strip()
    return value if len(re.sub(r"[\s\W_]+", "", value)) >= 4 else ""


def _visible(path: Path) -> bool:
    """False for blank strips: almost no dark pixels once put on white."""
    try:
        from PIL import Image
        with Image.open(path) as image:
            image = image.convert("RGBA")
            if image.height < 60:
                return False
            flat = Image.new("RGBA", image.size, "white")
            flat.alpha_composite(image)
            gray = flat.convert("L")
            dark = sum(gray.histogram()[:180])
            return dark / max(1, gray.width * gray.height) > 0.003
    except Exception:
        return False


def question_images(question: dict[str, Any], store: BankStore) -> dict[str, list[Path]]:
    cid = question.get("classroom_id")
    result: dict[str, Any] = {"stem": [], "explain": [], "slide": False}
    if not question.get("needs_vision") and not any(i.get("kind") == "body" for i in question.get("images") or []):
        return result
    images = [i for i in question.get("images") or [] if isinstance(i, dict) and i.get("path")]

    def path(image):
        try:
            return store.image_path(cid, image["path"])
        except (FileNotFoundError, KeyError, ValueError):
            return None

    body = [p for p in (path(i) for i in images if i.get("kind") == "body") if p]
    vision = str((question.get("vision") or {}).get("text") or "")
    slide_needed = (not _genuine(question.get("body")) or question.get("problem_type") == "subjective"
                    or question.get("type_text") == "主观题" or "附图" in vision)
    slides = [p for p in (path(i) for i in images if i.get("kind") == "slide") if p] if slide_needed else []
    result["stem"] = body or slides
    # A slide screenshot already shows the whole prompt with its options.
    result["slide"] = bool(slides) and not body
    # When the note's 答案 is already the faithful transcription of these
    # pictures (no structured answer in the bank), showing both would repeat it.
    if not (question.get("vision") or {}).get("answer_text") or question.get("answer"):
        result["explain"] = [p for p in (path(i) for i in images if i.get("kind") == "remark") if p and _visible(p)]
    return result


def has_images(question: dict[str, Any], store: BankStore | None = None) -> dict[str, int]:
    found = question_images(question, store or BankStore())
    return {"stem": len(found["stem"]), "explain": len(found["explain"]), "slide": found["slide"]}


def _materialize(source: Path, folder: Path, key: str) -> tuple[str, str]:
    from PIL import Image
    data = source.read_bytes()
    ident = "fig-" + hashlib.sha256(json.dumps(["yuketang", key, hashlib.sha256(data).hexdigest()]).encode()).hexdigest()[:16]
    relative = f"assets/{ident}.png"
    target = Path(folder) / relative
    if not target.is_file():
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGBA")
            flat = Image.new("RGBA", image.size, "white")
            flat.alpha_composite(image)
            out = io.BytesIO()
            flat.convert("RGB").save(out, "PNG", optimize=True)
        atomic_bytes(target, out.getvalue())
    return relative, hashlib.sha256(target.read_bytes()).hexdigest()


def registered(folder: Path) -> dict[str, str]:
    try:
        return {item["path"]: item["sha256"] for item in json.loads((Path(folder) / REGISTRY).read_text())}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _lookup(items: list[dict[str, Any]], store: BankStore, category: str) -> dict[str, dict[str, Any]]:
    wanted = {str(item.get("id")) for item in items if item.get("id")}
    found = {}
    for question in store.course_questions(category):
        if str(question.get("id")) in wanted:
            found[str(question["id"])] = question
    return found


def _entries(section: str) -> list[tuple[int, int]]:
    starts = [m.start() for m in re.finditer(r"^###\s*第\d+题", section, re.M)]
    return [(start, starts[i + 1] if i + 1 < len(starts) else len(section)) for i, start in enumerate(starts)]


def _rewrite_entry(entry: str, question: dict[str, Any], stem: list[str], explain: list[str], number: str,
                   slide: bool = False) -> str:
    lines = [line for line in entry.split("\n") if not _IMAGE_LINE.match(line.strip())]
    marker_at = next((i for i, line in enumerate(lines) if line.strip().startswith("<!-- yuketang:")), 0)
    out = lines[:marker_at + 1]
    rest = lines[marker_at + 1:]
    if stem:
        # The original image replaces the written stem; only genuine text stays.
        cut = next((i for i, line in enumerate(rest) if _OPTION.match(line) or line.strip().startswith("**答案**")), len(rest))
        out += [""]
        text = "" if slide else _genuine(question.get("body"))
        if text:
            out += [text, ""]
        out += [f"![第{number}题原题图片]({path})" for path in stem]
        out += [""]
        rest = rest[cut:]
        if slide:
            rest = [line for line in rest if not _OPTION.match(line)]
    if explain:
        at = next((i for i, line in enumerate(rest) if line.strip().startswith("**解析**")), None)
        if at is None:
            rest = [*rest, "", "**解析**：见下图。"]
            at = len(rest) - 1
        end = next((i for i in range(at + 1, len(rest)) if rest[i].strip().startswith(("### ", "**答案**")) or rest[i].strip() == "---"), len(rest))
        while end > at + 1 and not rest[end - 1].strip():
            end -= 1
        rest = rest[:end] + [""] + [f"![第{number}题解析图片]({path})" for path in explain] + [""] + rest[end:]
    text = "\n".join(out + rest)
    return re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n\n"


def attach(markdown: str, items: list[dict[str, Any]], folder: Path, category: str,
           store: BankStore | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Insert original images into each image question of 课上习题.

    Returns the markdown and a per-question report. Idempotent: earlier
    yuketang image lines are dropped and re-added from the bank.
    """
    from .retrieval import _SECTION
    store = store or BankStore()
    match = _SECTION.search(markdown or "")
    if not match or not items:
        return markdown, []
    by_marker = {str(item.get("题号")): item for item in items if item.get("题号")}
    bank = _lookup(items, store, category)
    head_end = match.end()
    following = re.search(r"^##\s", markdown[head_end:], re.M)
    end = head_end + following.start() if following else len(markdown)
    section = markdown[head_end:end]
    registry = {item_path: sha for item_path, sha in registered(folder).items()}
    report, pieces, last = [], [], 0
    for start, stop in _entries(section):
        entry = section[start:stop]
        marker = re.search(r"<!--\s*yuketang:(Y\d+)\s*-->", entry)
        number = re.match(r"###\s*第(\d+)题", entry)[1]
        item = by_marker.get(marker[1]) if marker else None
        question = bank.get(str(item.get("id"))) if item else None
        if not question:
            continue
        found = question_images(question, store)
        if not found["stem"] and not found["explain"]:
            continue
        stem, explain = [], []
        for role, paths, bucket in (("stem", found["stem"], stem), ("explain", found["explain"], explain)):
            for index, source in enumerate(paths):
                relative, sha = _materialize(source, folder, f"{question['id']}:{role}:{index}")
                registry[relative] = sha
                bucket.append(relative)
        pieces += [section[last:start], _rewrite_entry(entry, question, stem, explain, number, found["slide"])]
        last = stop
        report.append({"number": int(number), "marker": marker[1], "id": question["id"],
                       "stem_images": len(stem), "explain_images": len(explain)})
    if not report:
        return markdown, []
    pieces.append(section[last:])
    atomic_json(Path(folder) / REGISTRY, [{"path": p, "sha256": s} for p, s in sorted(registry.items())])
    new_section = "".join(pieces)
    tail = markdown[end:]
    return markdown[:head_end] + new_section.rstrip() + "\n" + ("\n" + tail.lstrip("\n") if tail.strip() else ""), report
