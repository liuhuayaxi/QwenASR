"""Local library for AI-generated course-note PDFs and prompt assembly.

The recording library owns source lessons.  This module deliberately keeps the
generated-note catalog separate: one upload always creates one immutable entry,
so a course can have several stage, midterm, and final-review PDFs built from
the same lessons.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .durable import atomic_json
from .paths import PRIVATE_ROOT
from .storage import TRASH_ROOT, local_now, safe_component
from .library_layout import NOTE_LIBRARY_MARKER


MAX_NOTE_BYTES = 250 * 1024 * 1024
NOTE_STATE_PATH = PRIVATE_ROOT / "note-library.json"

DEFAULT_NOTE_PROMPT = """你是大学课程复习笔记编辑。请根据我提供的课程课次线索，检索对应的闪念贝壳笔记，并结合资料库中该课程的全部上课 PDF，整理一份可独立复习的完整笔记。

课程：{{course}}
需要检索的闪念贝壳课堂笔记（标题与日期）：
{{lessons}}

要求：
1. 内容覆盖这些课次涉及的全部知识；老师课堂上明确强调的重点、易错点、考试要求和作业要求要清楚标出，不要用臆测补齐缺失信息。
2. 资料库中与本课程相关的 PDF 都要纳入核对。PPT 或课堂中提到的相应习题放在对应课次的最后，给出完整题目、来源和必要的解题思路，方便逐节练习；不能确认题目原文时要明确标注待核对。
3. 按章节顺序组织内容，并在章节或课次之间明确写出“第一节课”“第二节课”等边界；同一课程的多节课要能追溯到对应日期。
4. 输出 LaTeX 笔记，遵循工作目录中的 LaTeX 笔记整理规范。公式、符号、单位、图表和引用要能编译；先定义符号再使用，避免把转写中的口语或重复内容写进正文。
5. 删除只是在防止被质疑、表达客套或重复说明而没有推进论证的句子。不要默认认同我的判断；发现证据不足、概念混淆或逻辑错误时，直接指出并给出依据。

请只返回完整的 LaTeX 源码，并在文档开头给出课程、覆盖课次和资料范围。"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _date_for(metadata: dict[str, Any]) -> str:
    candidates: list[str] = []
    course_time = metadata.get("course_time")
    if isinstance(course_time, dict):
        candidates.extend([str(course_time.get("start") or ""), str(course_time.get("end") or "")])
    candidates.extend([str(metadata.get("recorded_at") or ""), str(metadata.get("started_at") or "")])
    for value in candidates:
        if not value.strip():
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            if len(value) >= 10 and value[4] == "-" and value[7] == "-":
                return value[:10]
    return "日期未知"


def _source_lesson(record: Any) -> dict[str, Any]:
    metadata = getattr(record, "metadata", {}) or {}
    sync = metadata.get("ideashell_sync") if isinstance(metadata.get("ideashell_sync"), dict) else {}
    return {
        "id": str(getattr(record, "id", metadata.get("id") or "")),
        "title": str(metadata.get("title") or "未命名课堂").strip() or "未命名课堂",
        "date": _date_for(metadata),
        "category": str(metadata.get("category") or "未分类"),
        "status": str(metadata.get("status") or ""),
        "ideashell_note_id": str(sync.get("note_id") or ""),
    }


class NoteLibrary:
    def __init__(self, settings: Any, recordings: Any) -> None:
        self.settings = settings
        self.recordings = recordings
        self.state_path = NOTE_STATE_PATH
        self.state = _read_json(self.state_path)
        saved_root = str(self.state.get("storage_root") or "").strip()
        # Keep the note catalog beside the user's course folders.  Its marker
        # prevents the shared-course scanner from treating it as a course.
        default_root = settings.storage_root / "AI生成笔记"
        self.storage_root = Path(saved_root).expanduser() if saved_root else default_root
        # Early development builds placed this directory at the Desktop level;
        # with no saved notes, move that generated default beside the course
        # folders so all material remains under the user's chosen root.
        legacy_root = Path.home() / "Desktop" / "AI生成笔记"
        if (saved_root and self.storage_root.resolve() == legacy_root.resolve()
                and not self.state.get("notes")):
            self.storage_root = default_root
        if not self.storage_root.is_absolute():
            self.storage_root = default_root
        self.storage_root = self.storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.storage_root / NOTE_LIBRARY_MARKER, {
            "kind": "qwenasr-note-library", "updated_at": local_now()
        })
        self.state.setdefault("notes", [])
        self.state.setdefault("prompt_template", DEFAULT_NOTE_PROMPT)
        self._save()

    def _save(self) -> None:
        self.state["storage_root"] = str(self.storage_root)
        self.state["updated_at"] = local_now()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.state_path, self.state)

    def template(self) -> str:
        value = self.state.get("prompt_template")
        return str(value) if isinstance(value, str) and value.strip() else DEFAULT_NOTE_PROMPT

    def set_template(self, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("提示词模板不能为空")
        if len(value) > 40_000:
            raise ValueError("提示词模板不能超过 40000 个字符")
        self.state["prompt_template"] = value
        self._save()
        return value

    def set_root(self, value: str) -> Path:
        root = Path(str(value or "").strip()).expanduser()
        if not root.is_absolute():
            raise ValueError("笔记保存位置必须填写绝对路径")
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise ValueError("笔记保存位置不可写")
        self.storage_root = root
        self._save()
        return root

    def categories(self) -> list[str]:
        names = list(self.settings.categories)
        for note in self.state.get("notes", []):
            category = str(note.get("category") or "未分类")
            if category not in names:
                names.append(category)
        return names or ["未分类"]

    def reclassify(self, category: str, target: str) -> bool:
        """Keep catalog course labels aligned without moving review PDFs."""
        if category == target:
            return False
        previous = copy.deepcopy(self.state)
        changed = False
        for note in self.state.get("notes", []):
            if isinstance(note, dict) and note.get("category") == category:
                note["category"] = target
                changed = True
        if changed:
            try:
                self._save()
            except BaseException:
                self.state = previous
                raise
        return changed

    def lessons(self, category: str = "") -> list[dict[str, Any]]:
        values = []
        for record in self.recordings.list_records():
            lesson = _source_lesson(record)
            if category and lesson["category"] != category:
                continue
            values.append(lesson)
        values.sort(key=lambda item: (item["date"], item["title"].casefold(), item["id"]))
        return values

    def _note_path(self, note: dict[str, Any]) -> Path:
        raw = Path(str(note.get("path") or ""))
        if not raw.is_absolute():
            raw = self.storage_root / raw
        return raw.resolve()

    def notes(self, category: str = "") -> list[dict[str, Any]]:
        result = []
        for note in self.state.get("notes", []):
            if not isinstance(note, dict):
                continue
            if category and str(note.get("category") or "未分类") != category:
                continue
            path = self._note_path(note)
            item = dict(note)
            item["exists"] = path.is_file()
            item["path"] = str(path)
            item["download_url"] = f"/api/notes/{note.get('id')}/download"
            result.append(item)
        result.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return result

    def add(self, category: str, title: str, filename: str, source_ids: list[str], path: Path) -> dict[str, Any]:
        safe_category = safe_component(category, "未分类")
        safe_title = safe_component(title or Path(filename).stem, "AI复习笔记")
        entry_id = uuid.uuid4().hex
        folder = self.storage_root / safe_category
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{safe_title}-{entry_id[:10]}.pdf"
        shutil.move(str(path), str(destination))
        source_set = {str(item) for item in source_ids if str(item).strip()}
        lessons = [item for item in self.lessons(safe_category) if item["id"] in source_set]
        entry = {
            "id": entry_id,
            "category": safe_category,
            "title": safe_title,
            "filename": destination.name,
            "path": str(destination),
            "created_at": local_now(),
            "source_session_ids": [item["id"] for item in lessons],
            "source_lessons": [{key: item[key] for key in ("id", "title", "date")} for item in lessons],
        }
        self.state.setdefault("notes", []).append(entry)
        self._save()
        return {**entry, "exists": True, "download_url": f"/api/notes/{entry_id}/download"}

    def remove(self, note_id: str) -> None:
        notes = self.state.get("notes", [])
        entry = next((item for item in notes if isinstance(item, dict) and item.get("id") == note_id), None)
        if entry is None:
            raise FileNotFoundError("没有找到这份笔记")
        path = self._note_path(entry)
        if path.exists():
            trash = TRASH_ROOT / ("笔记-" + uuid.uuid4().hex)
            trash.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(trash / path.name))
        self.state["notes"] = [item for item in notes if item is not entry]
        self._save()

    def prompt(self, category: str, source_ids: list[str], template: str | None = None) -> dict[str, Any]:
        selected = {str(value) for value in source_ids if str(value).strip()}
        if not selected:
            raise ValueError("请先选择至少一节课")
        lessons = [item for item in self.lessons(category) if item["id"] in selected]
        if len(lessons) != len(selected):
            raise ValueError("所选课次不存在，可能已被移动或删除；请刷新后重选")
        lessons.sort(key=lambda item: (item["date"], item["title"].casefold(), item["id"]))
        lines = "\n".join(f"- 标题：{item['title']}\n  日期：{item['date']}" for item in lessons)
        raw = str(template if template is not None else self.template()).strip()
        if not raw:
            raise ValueError("提示词模板不能为空")
        prompt = (raw.replace("{{course}}", category or "未分类")
                     .replace("{{lessons}}", lines)
                     .replace("{{课程}}", category or "未分类")
                     .replace("{{课次}}", lines))
        return {"prompt": prompt, "course": category or "未分类", "lessons": lessons}


def note_library_router(settings: Any, recordings: Any, *, library: NoteLibrary | None = None) -> APIRouter:
    library = library if library is not None else NoteLibrary(settings, recordings)
    router = APIRouter()

    @router.get("/api/notes")
    async def list_notes(category: str = "") -> dict[str, Any]:
        categories = library.categories()
        if category not in categories:
            category = categories[0] if categories else "未分类"
        return {"categories": categories, "category": category, "lessons": library.lessons(category),
                "notes": library.notes(category), "storage_root": str(library.storage_root),
                "prompt_template": library.template()}

    @router.post("/api/notes/upload")
    async def upload_note(category: str = Form(...), title: str = Form(""), source_ids: str = Form(""), file: UploadFile = File(...)) -> dict[str, Any]:
        filename = str(file.filename or "").strip()
        if Path(filename).suffix.lower() != ".pdf":
            raise HTTPException(400, "笔记必须是 PDF 文件")
        temporary = PRIVATE_ROOT / ("note-upload-" + uuid.uuid4().hex + ".pdf")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with temporary.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_NOTE_BYTES:
                        raise ValueError("单份笔记最多 250 MB")
                    output.write(chunk)
            if not size:
                raise ValueError("不能添加空 PDF")
            try:
                ids = json.loads(source_ids or "[]")
            except json.JSONDecodeError as exc:
                raise ValueError("课次选择数据无效") from exc
            if not isinstance(ids, list):
                raise ValueError("课次选择数据无效")
            entry = await asyncio.to_thread(library.add, category, title, filename, ids, temporary)
            return {"note": entry, "storage_root": str(library.storage_root)}
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    @router.get("/api/notes/{note_id}/download")
    async def download_note(note_id: str) -> FileResponse:
        entry = next((item for item in library.state.get("notes", []) if isinstance(item, dict) and item.get("id") == note_id), None)
        if entry is None:
            raise HTTPException(404, "没有找到这份笔记")
        path = library._note_path(entry)
        if not path.is_file():
            raise HTTPException(404, "笔记文件已不存在")
        return FileResponse(path, media_type="application/pdf", filename=str(entry.get("filename") or path.name), headers={"Cache-Control": "no-store"})

    @router.post("/api/notes/{note_id}/delete")
    async def delete_note(note_id: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(library.remove, note_id)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"deleted": True}

    @router.post("/api/notes/generate-prompt")
    async def generate_prompt(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return library.prompt(str(payload.get("category") or ""), payload.get("source_ids") or [], payload.get("template"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/notes/copy-prompt")
    async def copy_prompt(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        value = str(payload.get("prompt") or "")
        if not value.strip():
            raise HTTPException(400, "提示词为空，无法复制")
        if len(value) > 100_000:
            raise HTTPException(400, "提示词过长，无法复制")
        if sys.platform != "darwin":
            raise HTTPException(400, "当前系统没有可用的系统剪贴板，请手动复制结果框中的文字")
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["/usr/bin/pbcopy"],
                input=value,
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(400, f"系统剪贴板写入失败：{exc}") from exc
        return {"copied": True, "characters": len(value)}

    @router.put("/api/notes/settings")
    async def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            if "storage_root" in payload:
                library.set_root(str(payload.get("storage_root") or ""))
            if "prompt_template" in payload:
                library.set_template(str(payload.get("prompt_template") or ""))
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"storage_root": str(library.storage_root), "prompt_template": library.template()}

    @router.get("/api/notes/choose-root")
    async def choose_root() -> dict[str, Any]:
        if sys.platform != "darwin":
            raise HTTPException(400, "当前系统没有可用的原生文件夹选择器，请直接填写绝对路径")
        script = 'POSIX path of (choose folder with prompt "选择 AI 笔记保存文件夹" default location POSIX file "' + str(library.storage_root).replace('\\', '\\\\').replace('"', '\\"') + '")'
        result = await asyncio.to_thread(subprocess.run, ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False, timeout=60)
        if result.returncode:
            return {"cancelled": True}
        value = result.stdout.strip()
        return {"cancelled": not value, "storage_root": value}

    return router

