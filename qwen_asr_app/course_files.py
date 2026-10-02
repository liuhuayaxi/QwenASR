"""Course-owned originals, derived PDFs, and append-only attachment receipts."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

from .durable import atomic_json
from .storage import safe_component, local_now
from .library_layout import shared

MATERIALS = "课程资料"
MARKER = ".qwenasr-materials.json"
KINDS = ("课件", "讲义", "作业", "参考资料", "其他")
MAX_BYTES = 200 * 1024 * 1024


def digest_file(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class CourseFiles:
    def __init__(self, settings):
        self.settings = settings
        from .shared_files import SharedCourseFiles
        self.shared_store = SharedCourseFiles(settings)

    def folder(self, category, create=False):
        if shared(self.settings):
            return self.shared_store.folder(category, create)
        if category not in self.settings.categories or safe_component(category, "未分类") != category:
            raise ValueError("请先选择或新建课程")
        root = self.settings.storage_root.resolve()
        folder = root / category / MATERIALS
        if not folder.resolve().is_relative_to(root):
            raise ValueError("课程目录不能指向资料库外部")
        marker = folder / MARKER
        if folder.exists() and not marker.exists():
            raise ValueError("此课程已有同名“课程资料”目录，请先重命名该目录，避免混入其他文件")
        if create:
            folder.mkdir(parents=True, exist_ok=True)
            if not marker.exists():
                atomic_json(marker, {"format": "qwenasr-course-files", "version": 1})
        return folder

    def list(self, category):
        if shared(self.settings):
            return self.shared_store.list(category)
        folder = self.folder(category)
        if not folder.exists():
            return []
        result = []
        for path in folder.glob("*/asset.json"):
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                continue
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("id") == path.parent.name:
                    result.append(item)
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, category, asset_id):
        if shared(self.settings):
            return self.shared_store.get(category, asset_id)
        if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
            raise ValueError("文件编号无效")
        folder = self.folder(category)
        directory = folder / asset_id
        if not directory.resolve().is_relative_to(folder.resolve()):
            raise ValueError("文件目录无效")
        try:
            item = json.loads((directory / "asset.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("课程中没有找到这个文件") from None
        if item.get("id") != asset_id:
            raise ValueError("文件索引不一致")
        return item, directory

    def save(self, category, item):
        if shared(self.settings):
            return self.shared_store.save(category, item)
        _, directory = self.get(category, item["id"])
        atomic_json(directory / "asset.json", item)

    def import_file(self, category, temporary, filename, kind):
        if shared(self.settings):
            return self.shared_store.import_file(category, temporary, filename, kind)
        if kind not in KINDS:
            raise ValueError("文件类型无效")
        folder = self.folder(category, create=True)
        digest = digest_file(temporary)
        for item in self.list(category):
            if item["sha256"] == digest:
                return item, True
        # Keep the extension even when a very long filename needs shortening.
        raw_name = str(filename or "文件").replace("\\", "/").rsplit("/", 1)[-1]
        suffix = re.sub(r"[^.\w+-]", "_", Path(raw_name).suffix[:16])
        name = safe_component(Path(raw_name).stem, "文件") + suffix
        item = {"id": uuid.uuid4().hex, "name": name, "kind": kind,
                "sha256": digest, "size": Path(temporary).stat().st_size,
                "created_at": local_now(), "conversion": {"status": "none"}, "sync": []}
        directory = folder / item["id"]
        original = directory / "原件" / name
        original.parent.mkdir(parents=True)
        try:
            shutil.copyfile(temporary, original)
            atomic_json(directory / "asset.json", item)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return item, False

    def file(self, category, asset_id, variant="original"):
        if shared(self.settings):
            return self.shared_store.file(category, asset_id, variant)
        item, directory = self.get(category, asset_id)
        if variant == "original":
            path = directory / "原件" / item["name"]
        elif variant == "pdf":
            conversion = item.get("conversion", {})
            if conversion.get("status") != "completed":
                raise ValueError("请先生成 PDF 版本")
            path = directory / "PDF" / conversion["name"]
        else:
            raise ValueError("请选择原件或 PDF")
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError("文件不存在或已移出课程目录")
        return item, path
