"""Local, private storage for the Yuketang question bank.

Layout (default root ``<project>/local-data/yuketang``; ``QWEN_ASR_DATA_DIR`` and
``QWEN_ASR_YUKETANG_DIR`` relocate it)::

    browser-profile/                 persistent Chrome profile (login session)
    bindings.json                    {"courses": {category: [{classroom_id, university_id, name}]}}
    classrooms/<classroom_id>/
        classroom.json               course name, university id, last sync, counts
        activities/<activity_id>.json  sync state: fingerprint, status, error
        questions/<activity_id>.json   normalized questions of that activity
        raw/<activity_id>/<name>.json  verbatim API responses (local only)
        images/<activity_id>/<file>    slide / remark / body images
        vision/<sha256>.json           cached image transcriptions

The bank never lives inside the course library folder: it holds the student's
own answers and scores, and the library may be synced to a cloud drive.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from ..durable import atomic_bytes, atomic_json
from ..paths import PRIVATE_ROOT
from .parsing import search_text

_LOCK = threading.RLock()
CLASSROOM_ID = re.compile(r"^\d{1,15}$")


def default_root() -> Path:
    override = os.getenv("QWEN_ASR_YUKETANG_DIR")
    return Path(override).expanduser() if override else PRIVATE_ROOT / "yuketang"


def now_iso() -> str:
    from ..storage import local_now
    return local_now()


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def safe_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))[:120].strip("._")
    return text or "item"


def classroom_id(value: Any) -> str:
    text = str(value or "").strip()
    if not CLASSROOM_ID.match(text):
        raise ValueError("雨课堂班级编号必须是数字（例如 26260946）")
    return text


class BankStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_root()

    # ------------------------------------------------------------ locations
    @property
    def profile_dir(self) -> Path:
        return self.root / "browser-profile"

    def classroom_dir(self, cid: Any) -> Path:
        return self.root / "classrooms" / classroom_id(cid)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    # ------------------------------------------------------------ bindings
    @property
    def bindings_path(self) -> Path:
        return self.root / "bindings.json"

    def bindings(self) -> dict[str, list[dict[str, Any]]]:
        data = _read(self.bindings_path, {})
        courses = data.get("courses") if isinstance(data, dict) else {}
        result = {}
        for category, items in (courses or {}).items():
            clean = [item for item in items or [] if isinstance(item, dict) and CLASSROOM_ID.match(str(item.get("classroom_id") or ""))]
            if clean:
                result[str(category)] = clean
        return result

    def classrooms_for(self, category: str) -> list[dict[str, Any]]:
        return list(self.bindings().get(str(category), []))

    # ------------------------------------------------------------ course list
    @property
    def course_list_path(self) -> Path:
        return self.root / "courses.json"

    def save_course_list(self, courses: list[dict[str, Any]]) -> dict[str, Any]:
        value = {"version": 1, "fetched_at": now_iso(), "courses": courses}
        with _LOCK:
            self.ensure()
            atomic_json(self.course_list_path, value)
        return value

    def course_list(self) -> dict[str, Any]:
        data = _read(self.course_list_path, {})
        if not isinstance(data, dict) or not isinstance(data.get("courses"), list):
            return {"fetched_at": "", "courses": []}
        return {"fetched_at": str(data.get("fetched_at") or ""), "courses": [c for c in data["courses"] if isinstance(c, dict)]}

    def bind(self, category: str, cid: Any, *, university_id: Any = None, name: str = "",
             replace: bool = False) -> dict[str, Any]:
        category = str(category or "").strip()
        if not category:
            raise ValueError("请先选择 QwenASR 课程")
        cid = classroom_id(cid)
        with _LOCK:
            self.ensure()
            data = self.bindings()
            # 改绑 replaces every earlier classroom of this course.
            items = [] if replace else [item for item in data.get(category, []) if str(item.get("classroom_id")) != cid]
            entry = {"classroom_id": cid, "bound_at": now_iso()}
            if university_id not in (None, ""):
                entry["university_id"] = str(int(str(university_id)))
            if name:
                entry["name"] = str(name)[:120]
            items.append(entry)
            data[category] = items
            atomic_json(self.bindings_path, {"version": 1, "courses": data})
            return entry

    def unbind(self, category: str, cid: Any) -> bool:
        cid = classroom_id(cid)
        with _LOCK:
            data = self.bindings()
            items = data.get(str(category), [])
            kept = [item for item in items if str(item.get("classroom_id")) != cid]
            if len(kept) == len(items):
                return False
            if kept:
                data[str(category)] = kept
            else:
                data.pop(str(category), None)
            atomic_json(self.bindings_path, {"version": 1, "courses": data})
            return True

    def rename_category(self, old: str, new: str | None) -> None:
        """Follow QwenASR course rename/delete so bindings never dangle."""
        with _LOCK:
            data = self.bindings()
            if old not in data:
                return
            items = data.pop(old)
            if new:
                merged = {str(item["classroom_id"]): item for item in [*data.get(new, []), *items]}
                data[new] = list(merged.values())
            atomic_json(self.bindings_path, {"version": 1, "courses": data})

    def categories_for(self, cid: Any) -> list[str]:
        cid = str(cid)
        return [category for category, items in self.bindings().items()
                if any(str(item.get("classroom_id")) == cid for item in items)]

    # ------------------------------------------------------------ classroom data
    def classroom(self, cid: Any) -> dict[str, Any]:
        return _read(self.classroom_dir(cid) / "classroom.json", {})

    def save_classroom(self, cid: Any, value: dict[str, Any]) -> None:
        folder = self.classroom_dir(cid)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / "classroom.json", value)

    def activity_state(self, cid: Any, activity_id: Any) -> dict[str, Any]:
        return _read(self.classroom_dir(cid) / "activities" / f"{safe_name(activity_id)}.json", {})

    def save_activity_state(self, cid: Any, activity_id: Any, value: dict[str, Any]) -> None:
        atomic_json(self.classroom_dir(cid) / "activities" / f"{safe_name(activity_id)}.json", value)

    def activity_states(self, cid: Any) -> list[dict[str, Any]]:
        folder = self.classroom_dir(cid) / "activities"
        return [_read(path, {}) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []

    def save_raw(self, cid: Any, activity_id: Any, name: str, payload: Any) -> None:
        atomic_json(self.classroom_dir(cid) / "raw" / safe_name(activity_id) / f"{safe_name(name)}.json", payload)

    def raw(self, cid: Any, activity_id: Any, name: str) -> Any:
        return _read(self.classroom_dir(cid) / "raw" / safe_name(activity_id) / f"{safe_name(name)}.json", None)

    def save_questions(self, cid: Any, activity_id: Any, questions: list[dict[str, Any]]) -> None:
        path = self.classroom_dir(cid) / "questions" / f"{safe_name(activity_id)}.json"
        previous = {item.get("id"): item for item in _read(path, []) if isinstance(item, dict)}
        for question in questions:
            # Keep finished vision transcriptions and downloaded files when an
            # unchanged question is re-imported.
            old = previous.get(question.get("id")) or {}
            if old.get("vision") and not question.get("vision"):
                question["vision"] = old["vision"]
            old_images = {image.get("source"): image for image in old.get("images") or [] if isinstance(image, dict)}
            for image in question.get("images") or []:
                kept = old_images.get(image.get("source"))
                if kept and kept.get("path") and not image.get("path"):
                    image.update({k: kept[k] for k in ("path", "sha256") if k in kept})
        atomic_json(path, questions)

    def questions(self, cid: Any) -> list[dict[str, Any]]:
        folder = self.classroom_dir(cid) / "questions"
        result: list[dict[str, Any]] = []
        if not folder.is_dir():
            return result
        for path in sorted(folder.glob("*.json")):
            items = _read(path, [])
            result.extend(item for item in items if isinstance(item, dict))
        return result

    def update_question(self, cid: Any, question_id: str, updates: dict[str, Any]) -> bool:
        folder = self.classroom_dir(cid) / "questions"
        with _LOCK:
            for path in sorted(folder.glob("*.json")):
                items = _read(path, [])
                for item in items:
                    if isinstance(item, dict) and item.get("id") == question_id:
                        item.update(updates)
                        atomic_json(path, items)
                        return True
        return False

    def save_image(self, cid: Any, activity_id: Any, name: str, data: bytes) -> dict[str, str]:
        relative = Path("images") / safe_name(activity_id) / safe_name(name)
        path = self.classroom_dir(cid) / relative
        atomic_bytes(path, data)
        return {"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()}

    def image_path(self, cid: Any, relative: str) -> Path:
        folder = self.classroom_dir(cid).resolve()
        path = (folder / str(relative)).resolve()
        if not path.is_relative_to(folder / "images") or not path.is_file():
            raise FileNotFoundError(relative)
        return path

    # ------------------------------------------------------------ course views
    def course_questions(self, category: str) -> list[dict[str, Any]]:
        result = []
        for item in self.classrooms_for(category):
            result.extend(self.questions(item["classroom_id"]))
        return result

    def counts(self, questions: Iterable[dict[str, Any]]) -> dict[str, int]:
        counts = {"exam": 0, "lesson": 0, "courseware": 0, "total": 0, "answer_known": 0,
                  "needs_vision": 0, "vision_done": 0, "wrong": 0}
        for question in questions:
            counts["total"] += 1
            counts[question.get("bank_category") or "exam"] = counts.get(question.get("bank_category") or "exam", 0) + 1
            counts["answer_known"] += bool(question.get("answer_known"))
            if question.get("needs_vision"):
                counts["needs_vision"] += 1
                counts["vision_done"] += (question.get("vision") or {}).get("status") == "done"
            counts["wrong"] += question.get("correct") is False
        return counts

    def search_ranked(self, category: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Natural-language search for the course 检索 box: CJK-bigram overlap
        with the query, exact substring hits first."""
        from .retrieval import _tokens
        query = str(query or "").strip()
        wanted = _tokens(query)
        if not query or not wanted:
            return []
        compact_query = re.sub(r"\s+", "", query).lower()
        scored = []
        for question in self.course_questions(category):
            text = search_text(question).lower()
            exact = compact_query in re.sub(r"\s+", "", text)
            overlap = len(wanted & _tokens(text)) / len(wanted)
            if exact or overlap >= 0.6:
                scored.append((exact, overlap, question))
        scored.sort(key=lambda item: (item[0], item[1], item[2].get("date") or ""), reverse=True)
        return [{**question, "_score": round(overlap + exact, 3)} for exact, overlap, question in scored[:max(1, min(50, int(limit)))]]

    def search(self, category: str, query: str = "", source: str = "", limit: int = 200) -> list[dict[str, Any]]:
        questions = self.course_questions(category)
        terms = [term for term in re.split(r"\s+", str(query or "").strip().lower()) if term]
        if source:
            questions = [q for q in questions if q.get("bank_category") == source or q.get("source") == source]
        if terms:
            questions = [q for q in questions if all(term in search_text(q).lower() for term in terms)]
        questions.sort(key=lambda q: (q.get("date") or "", q.get("activity_id") or "", q.get("page") or q.get("index") or 0), reverse=True)
        return questions[:max(1, min(1000, int(limit)))]
