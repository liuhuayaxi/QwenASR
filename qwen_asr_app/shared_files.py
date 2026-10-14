"""Index the actual course folder; the UI and Finder edit the same files."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
import time
from threading import RLock
from functools import wraps

def serialized(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return run
from pathlib import Path

from .durable import atomic_json
from .library_layout import AI_FILES, AI_NOTES, INDEX, RECORDINGS
from .storage import TRASH_ROOT, local_now, safe_component


def legacy_output_key(filename):
    """Normalize old AI output names for a unique source-file fallback match."""
    stem = Path(filename).stem
    stem = re.sub(r"\(AI\)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\([^)]*\)", "", stem)
    # Old course names sometimes contained a standalone A/B/C marker. Keep
    # letters embedded in real words (for example, AIGC) intact.
    stem = re.sub(r"(?<![A-Za-z])[ABC](?![A-Za-z])", "", stem, flags=re.IGNORECASE)
    return re.sub(r"[\W_]+", "", stem)


class SharedCourseFiles:
    def __init__(self, settings):
        self.settings = settings
        self.lock = RLock()
        self.cache = {}

    def folder(self, category, create=False):
        if category not in self.settings.categories or safe_component(category, "") != category:
            raise ValueError("课程分类已变化，请刷新后重试")
        root = self.settings.storage_root.resolve()
        folder = root / category
        if folder.is_symlink() or not folder.resolve().is_relative_to(root):
            raise ValueError("课程目录不能指向资料库外部")
        if create:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / RECORDINGS).mkdir(exist_ok=True)
        return folder

    def _read(self, folder):
        try:
            value = json.loads((folder / INDEX / "index.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    def _write(self, folder, items):
        atomic_json(folder / INDEX / "index.json", items)
        self.cache.clear()

    @staticmethod
    def _stamp(path):
        stat = path.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]

    def _path(self, folder, relative):
        path = folder / str(relative)
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("文件路径不在当前课程目录中")
        return path

    @serialized
    def scan(self, category):
        cached = self.cache.get(category)
        if cached and time.monotonic() - cached[0] < 1:
            return copy.deepcopy(cached[1])
        folder = self.folder(category)
        if not folder.exists():
            return []
        items = self._read(folder)
        before = json.dumps(items, ensure_ascii=False, sort_keys=True)
        by_path = {item.get("relative_path"): item for item in items.values()}
        by_inode = {tuple(item["stamp"][:2]): item for item in items.values() if item.get("stamp")}
        seen = set()
        pdfs = []
        for root, directories, files in os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories if not name.startswith('.')
                              and name not in {RECORDINGS, AI_FILES, AI_NOTES, "课程资料"}
                              and not (Path(root) / name).is_symlink()]
            for name in files:
                if name.startswith('.') or name.startswith('~$') or name.endswith(('.tmp', '.part')):
                    continue
                path = Path(root) / name
                if path.is_symlink():
                    continue
                try:
                    stamp = self._stamp(path)
                except FileNotFoundError:
                    continue
                relative = path.relative_to(folder).as_posix()
                item = by_path.get(relative) or by_inode.get(tuple(stamp[:2]))
                if item is None or item.get("id") in seen:
                    ident = uuid.uuid4().hex
                    item = {"id": ident, "kind": "其他", "created_at": local_now(), "sync": [], "conversion": {"status": "none"}}
                    items[ident] = item
                previous = item.get("stamp")
                if previous and previous[2:] != stamp[2:]:
                    item["sha256"] = ""
                    if item.get("conversion", {}).get("status") == "completed":
                        item["conversion"].update(status="stale", stage="原件已更新，可重新生成 AI 友好型文件")
                item.update(name=name, relative_path=relative, stamp=stamp, size=stamp[2], missing=False)
                item.setdefault("sha256", "")
                seen.add(item["id"])
        ai_folder = folder / AI_FILES
        if ai_folder.is_dir() and not ai_folder.is_symlink():
            pdfs = [path for path in ai_folder.rglob('*.pdf') if not path.is_symlink()
                    and not any(part.startswith('.') for part in path.relative_to(ai_folder).parts)]
        ai_by_path = {path.relative_to(folder).as_posix(): path for path in pdfs}
        ai_by_inode = {tuple(self._stamp(path)[:2]): path for path in pdfs if path.exists()}
        claimed = {item.get("conversion", {}).get("relative_path") for item in items.values()}
        unclaimed_by_key = {}
        for path in pdfs:
            relative = path.relative_to(folder).as_posix()
            if relative not in claimed:
                unclaimed_by_key.setdefault(legacy_output_key(path.name), []).append(path)
        for item in sorted(items.values(), key=lambda item: (Path(item.get("name", "")).suffix.lower() != '.pdf', item["id"])):
            item["missing"] = item["id"] not in seen
            if item["missing"]:
                continue
            conversion = item.setdefault("conversion", {"status": "none"})
            if conversion.get("status") in {"queued", "running", "stopping"}:
                continue
            path = ai_by_path.get(conversion.get("relative_path"))
            if path is None and conversion.get("stamp"):
                path = ai_by_inode.get(tuple(conversion["stamp"][:2]))
            if path is None and not conversion.get("relative_path"):
                original = Path(item["relative_path"])
                candidate = ai_folder / original.parent / (original.stem + '(AI).pdf')
                relative = candidate.relative_to(folder).as_posix()
                if relative not in claimed and relative in ai_by_path:
                    path = candidate
                    claimed.add(relative)
            if path is None and not conversion.get("relative_path"):
                matches = [candidate for candidate in unclaimed_by_key.get(legacy_output_key(item.get("name", "")), [])
                           if candidate.relative_to(folder).as_posix() not in claimed]
                if len(matches) == 1:
                    path = matches[0]
                    claimed.add(path.relative_to(folder).as_posix())
            if path is not None and path.exists():
                stamp = self._stamp(path)
                changed = conversion.get("stamp") != stamp
                conversion.update(name=path.name, relative_path=path.relative_to(folder).as_posix(), stamp=stamp)
                if conversion.get("status") in {"none", "completed"} or (changed and stamp[3] > item['stamp'][3]):
                    conversion["status"] = "completed"
                if changed:
                    conversion.update(sha256="", external=True, stage="已同步文件夹中的 AI 友好型文件")
            elif conversion.get("status") in {"completed", "stale"}:
                conversion.update(status="none", stage="AI 友好型文件已从文件夹移除")
        if before != json.dumps(items, ensure_ascii=False, sort_keys=True):
            self._write(folder, items)
        result = sorted((copy.deepcopy(item) for item in items.values() if not item.get("missing")),
                        key=lambda item: item.get("relative_path", "").casefold())
        self.cache[category] = (time.monotonic(), result)
        return result

    def list(self, category):
        return self.scan(category)

    @serialized
    def get(self, category, asset_id):
        if not isinstance(asset_id, str) or not re.fullmatch(r"[a-f0-9]{32}", asset_id):
            raise ValueError("文件编号无效")
        self.scan(category)
        folder = self.folder(category)
        item = self._read(folder).get(asset_id)
        if not item or item.get("missing"):
            raise ValueError("文件已删除或移到其他课程，请刷新文件列表")
        return item, folder

    @serialized
    def save(self, category, item):
        folder = self.folder(category)
        values = self._read(folder)
        if item["id"] not in values:
            raise ValueError("文件索引已变化，请刷新后重试")
        values[item["id"]] = item
        self._write(folder, values)

    def file(self, category, asset_id, variant="original"):
        item, folder = self.get(category, asset_id)
        relative = item["relative_path"] if variant == "original" else item.get("conversion", {}).get("relative_path")
        if variant not in {"original", "pdf"} or not relative:
            raise ValueError("文件版本尚未生成")
        path = self._path(folder, relative)
        if not path.is_file():
            raise ValueError("文件已经移走，请刷新后重试")
        return item, path

    def output_path(self, category, item):
        folder = self.folder(category)
        if item.get("conversion", {}).get("relative_path"):
            return self._path(folder, item["conversion"]["relative_path"])
        source = Path(item["relative_path"])
        target = folder / AI_FILES / source.parent / (source.stem + '(AI).pdf')
        claimed = {value.get("conversion", {}).get("relative_path") for value in self._read(folder).values() if value['id'] != item['id']}
        if target.relative_to(folder).as_posix() in claimed:
            target = target.with_name(source.name + '(AI).pdf')
        return self._path(folder, target.relative_to(folder))

    @serialized
    def import_file(self, category, temporary, filename, kind):
        from .course_files import KINDS, digest_file
        if kind not in KINDS:
            raise ValueError("文件类型无效")
        folder = self.folder(category, create=True)
        size = Path(temporary).stat().st_size
        digest = digest_file(temporary)
        for item in self.scan(category):
            if item["size"] == size and (item.get('sha256') or digest_file(self._path(folder, item['relative_path']))) == digest:
                return item, True
        raw = str(filename or "文件").replace('\\', '/').rsplit('/', 1)[-1]
        name = safe_component(Path(raw).stem, "文件") + Path(raw).suffix[:16]
        target = folder / name
        index = 2
        while target.exists():
            target = folder / f"{Path(name).stem}（{index}）{Path(name).suffix}"
            index += 1
        work = folder / ('.upload-' + uuid.uuid4().hex)
        try:
            shutil.copyfile(temporary, work)
            work.replace(target)
        finally:
            work.unlink(missing_ok=True)
        self.cache.clear()
        item = next(item for item in self.scan(category) if item["relative_path"] == target.name)
        item.update(kind=kind, sha256=digest)
        self.save(category, item)
        return item, False

    @serialized
    def rename(self, category, asset_id, name):
        item, source = self.file(category, asset_id)
        if not name or safe_component(name, "") != name or '/' in name or '\\' in name:
            raise ValueError("请输入有效的文件名（最多 80 个字符）")
        if Path(name).suffix.lower() != source.suffix.lower():
            raise ValueError("重命名时请保留原文件扩展名")
        target = source.with_name(name)
        if target == source:
            return item
        if target.exists():
            raise ValueError("已有同名文件，请使用其他名称")
        source.rename(target)
        self.cache.clear()
        return next(value for value in self.scan(category) if value['id'] == asset_id)

    @serialized
    def delete(self, category, asset_id):
        item, source = self.file(category, asset_id)
        target = TRASH_ROOT / ('课程资料-' + uuid.uuid4().hex)
        target.mkdir(parents=True)
        moved = []
        try:
            paths = [source]
            if item.get('conversion', {}).get('relative_path'):
                try:
                    _, pdf = self.file(category, asset_id, 'pdf')
                    paths.append(pdf)
                except ValueError:
                    pass
            for path in paths:
                destination = target / (AI_FILES if path != source else '原件') / path.name
                destination.parent.mkdir(exist_ok=True)
                shutil.move(str(path), str(destination))
                moved.append((path, destination))
        except BaseException:
            for original, destination in reversed(moved):
                shutil.move(str(destination), str(original))
            raise
        self.cache.clear()
        self.scan(category)
        return {"deleted": True, "recoverable": True}
