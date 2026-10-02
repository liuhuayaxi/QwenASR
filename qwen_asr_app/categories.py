"""Move local category contents without deleting recordings or course materials."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path

from .course_files import MARKER, MATERIALS
from .durable import atomic_bytes, atomic_json
from .storage import RecordingRecord, local_now, safe_component
from .library_layout import shared


def change_category(settings, recordings, category: str, name: str | None = None):
    if shared(settings):
        from .shared_library import change_shared_category
        return change_shared_category(settings, recordings, category, name)
    deleting = name is None
    if category not in settings.categories:
        raise ValueError("这个分类已不存在，请刷新后重试")
    if category == "未分类":
        raise ValueError("“未分类”是默认分类，不能重命名或删除")
    target_name = "未分类" if deleting else str(name).strip()
    if not target_name or safe_component(target_name, "") != target_name or "\\" in target_name:
        raise ValueError("分类名称须为 1–80 个字符，不能包含斜杠、冒号或其他无效字符")
    if target_name == category:
        return {"category": category, "deleted": False, "recordings_moved": 0}
    if not deleting and any(value.casefold() == target_name.casefold() for value in settings.categories):
        raise ValueError("已有同名分类，请使用其他名称")

    records = [record for record in recordings.list_records() if record.metadata.get("category") == category]
    if any(record.metadata.get("status") in {"active", "processing"} for record in records):
        raise ValueError("此分类仍有录音或转写任务，请完成后再操作")
    roots = {Path(value).expanduser().resolve() for value in settings.known_roots}
    roots.add(settings.storage_root.resolve())
    moves: list[tuple[Path, Path]] = []
    profile_changes: list[tuple[Path, Path | None, dict]] = []
    merged_materials: list[Path] = []
    old_folders: set[Path] = set()
    record_targets: list[tuple[RecordingRecord, Path]] = []

    def check_path(path: Path, root: Path):
        # Never traverse aliases to another category or to unrelated files.
        if not path.is_relative_to(root):
            raise ValueError("分类目录不在资料库内")
        for candidate in (path, *path.parents):
            if candidate == root:
                break
            if candidate.is_symlink():
                raise ValueError(f"分类目录包含符号链接，请先在 Finder 中处理：{candidate.name}")

    def plan_move(source: Path, target: Path):
        if target.exists() or target.is_symlink() or any(target == other for _, other in moves):
            raise ValueError(f"目标分类已有同名内容，未移动任何文件：{target.name}")
        moves.append((source, target))

    for root in sorted(roots):
        source_folder, target_folder = root / category, root / target_name
        check_path(source_folder, root)
        check_path(target_folder, root)
        if not deleting and target_folder.exists():
            raise ValueError(f"资料库中已有同名目录，请使用其他名称：{target_name}")
        old_folders.add(source_folder)
        source_materials, target_materials = source_folder / MATERIALS, target_folder / MATERIALS
        if (source_materials / MARKER).is_file():
            check_path(source_materials / MARKER, root)
            check_path(target_materials, root)
            if target_materials.exists():
                check_path(target_materials / MARKER, root)
                if not (target_materials / MARKER).is_file():
                    raise ValueError("目标分类的“课程资料”目录不是应用管理的目录，无法合并")
                for child in sorted(source_materials.iterdir()):
                    if child.name != MARKER:
                        plan_move(child, target_materials / child.name)
                merged_materials.append(source_materials)
            else:
                plan_move(source_materials, target_materials)
        profile_path = source_folder / ".qwenasr-course.json"
        if profile_path.exists():
            check_path(profile_path, root)
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            if not isinstance(profile, dict):
                raise ValueError("课程配置格式无效，请先修复后再操作")
            target_profile = None if deleting else target_folder / profile_path.name
            if target_profile is not None:
                plan_move(profile_path, target_profile)
            profile_changes.append((profile_path, target_profile, profile))

    for record in records:
        root = next((value for value in roots if record.directory.parent == value / category), None)
        # Legacy recordings can live outside a named category directory.
        destination_root = root or settings.storage_root.resolve()
        target = destination_root / target_name / record.directory.name
        check_path(record.directory, root or record.directory.parent)
        check_path(target, destination_root)
        plan_move(record.directory, target)
        record_targets.append((record, target))

    # Only category metadata changes; recordings, note text and originals move
    # intact. Keep backups until both the moves and settings write succeed.
    backup = Path(tempfile.mkdtemp(prefix="qwenasr-category-"))
    snapshots: dict[Path, Path | None] = {}
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    previous_categories = list(settings.categories)
    previous_profiles = copy.deepcopy(settings.category_profiles)
    preserve_backup = False

    def snapshot(path):
        if path in snapshots:
            return
        if path.is_symlink():
            raise ValueError(f"文件包含符号链接，无法修改：{path.name}")
        saved = backup / str(len(snapshots)) if path.exists() else None
        if saved is not None:
            shutil.copyfile(path, saved)
        snapshots[path] = saved

    def mkdir(path):
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for folder in reversed(missing):
            folder.mkdir()
            created.append(folder)

    try:
        for record in records:
            snapshot(record.meta_path)
        for source, _, _ in profile_changes:
            snapshot(source)
        for source, target in moves:
            mkdir(target.parent)
            shutil.move(str(source), str(target))
            moved.append((source, target))
        for _, target in record_targets:
            record = RecordingRecord.load(target)
            record.metadata.update(category=target_name, updated_at=local_now())
            options = record.metadata.get("batch_options")
            if isinstance(options, dict) and options.get("category") == category:
                options["category"] = target_name
            sync = record.metadata.get("ideashell_sync")
            if isinstance(sync, dict) and sync.get("note_id"):
                sync["status"] = "local_changes"
            record.save_metadata()
        for source, target, profile in profile_changes:
            if target is None:
                source.unlink()
            else:
                atomic_json(target, {**profile, "category": target_name})
        settings.categories = [target_name if value == category and not deleting else value
                               for value in settings.categories if not (deleting and value == category)]
        settings.categories.sort(key=lambda value: (value != "未分类", value.casefold()))
        profile = settings.category_profiles.pop(category, None)
        if not deleting and profile is not None:
            settings.category_profiles[target_name] = profile
        settings.save()
    except BaseException as exc:
        errors = []
        for source, target in reversed(moved):
            try:
                shutil.move(str(target), str(source))
            except OSError as rollback_error:
                errors.append(str(rollback_error))
        for path, saved in snapshots.items():
            try:
                if saved is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_bytes(path, saved.read_bytes())
            except OSError as rollback_error:
                errors.append(str(rollback_error))
        settings.categories, settings.category_profiles = previous_categories, previous_profiles
        try:
            settings.save()
        except OSError as rollback_error:
            errors.append(str(rollback_error))
        for folder in reversed(created):
            try:
                folder.rmdir()
            except OSError:
                pass
        if errors:
            preserve_backup = True
            atomic_json(backup / "recovery.json", {
                "files": {str(path): str(saved) if saved else None for path, saved in snapshots.items()},
                "moves": [[str(source), str(target)] for source, target in moved], "errors": errors,
            })
            raise RuntimeError(f"分类更新未完成，恢复备份保存在 {backup}；请保留备份后检查磁盘权限") from exc
        raise
    finally:
        if not preserve_backup:
            shutil.rmtree(backup, ignore_errors=True)

    # Remove only empty managed containers. Unrelated local files stay in place.
    for folder in merged_materials:
        try:
            if all(child.name == MARKER for child in folder.iterdir()):
                (folder / MARKER).unlink(missing_ok=True)
                folder.rmdir()
        except OSError:
            pass
    for folder in old_folders:
        try:
            folder.rmdir()
        except OSError:
            pass
    recordings.refresh()
    return {"category": target_name, "deleted": deleting, "recordings_moved": len(records)}
