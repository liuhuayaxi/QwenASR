"""Stream complete personal-data backups to a private, downloadable ZIP."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .paths import PRIVATE_ROOT, ROOT
from .storage import LEGACY_RECORDINGS_DIR, SETTINGS_PATH, local_now
from .media_retention import disposable_name


@dataclass(frozen=True)
class BackupSource:
    path: Path
    prefix: str
    excluded: tuple[str, ...] = ()


def backup_sources(settings, recordings, notes) -> list[BackupSource]:
    sources = []

    def add(path, prefix, excluded=()):
        path = Path(path).expanduser().absolute()
        path = path.parent.resolve() / path.name if path.is_symlink() else path.resolve()
        if not path.exists() and not path.is_symlink():
            return
        if any(path == item.path or path.is_relative_to(item.path) for item in sources):
            return
        sources.append(BackupSource(path, prefix, tuple(excluded)))

    # SQLite indexes are derived and may change during read-only searches.
    # Preserve authoritative sources and note histories; rebuild the index on restore.
    add(settings.storage_root, "资料库", (".knowledge.db", ".knowledge.db-wal", ".knowledge.db-shm"))
    add(PRIVATE_ROOT, "配置/private", (
        "runtimes", "mineru4/models", "services", "backups", "compat-backups",
        "library-migrations", "repair-backups", "mineru-upgrade-backups",
        "remote-operations", "tasks", "uploads", "library-last-index.json",
        "smartclass/queue.json", "smartclass/imports.json",
    ))
    add(SETTINGS_PATH, "配置/app-settings.json")
    for path in sorted(SETTINGS_PATH.parent.glob(SETTINGS_PATH.name + ".*")):
        add(path, "配置/" + path.name)
    add(ROOT / ".env", "配置/.env")
    for index, record in enumerate(recordings.list_records()):
        add(record.directory, f"其他资料/外部录音/{index:04d}-{record.directory.name}")
    # Old catalogs may reference files outside the current root.
    for index, note in enumerate(notes.notes()):
        add(note["path"], f"其他资料/外部笔记/{index:04d}-{Path(note['path']).name}")
    # A custom gateway token location also belongs in a complete configuration.
    from .inference_providers import inference_settings
    token_file = inference_settings.read().get("token_file")
    if token_file:
        add(token_file, "配置/外部凭证/remote-infer-token")
    return sources


def _stamp(path):
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns


def inventory(sources: list[BackupSource]):
    entries = []
    for source in sources:
        def include(path):
            relative = path.relative_to(source.path).as_posix()
            return (not disposable_name(path.name) and path.name != '.note-history' and not any(
                relative == value or relative.startswith(value + "/") for value in source.excluded))

        def entry(path):
            relative = path.relative_to(source.path).as_posix()
            name = source.prefix + ("/" + relative if relative != "." else "")
            stamp = _stamp(path)
            mode = stamp[2]
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise ValueError(f"资料包含无法备份的特殊文件：{path.name}")
            entries.append((path, name, stamp))

        entry(source.path)
        if source.path.is_dir() and not source.path.is_symlink():
            def failed(error):
                raise error
            for root, directories, files in os.walk(source.path, followlinks=False, onerror=failed):
                directories[:] = sorted(name for name in directories if include(Path(root) / name))
                for name in directories:
                    entry(Path(root) / name)
                for name in sorted(files):
                    path = Path(root) / name
                    if include(path):
                        entry(path)
    return entries


def build_backup(sources, destination: Path, update: Callable, *, check_space=True):
    entries = inventory(sources)
    total = sum(stamp[3] for _, _, stamp in entries if stat.S_ISREG(stamp[2]))
    count = sum(not stat.S_ISDIR(stamp[2]) for _, _, stamp in entries)
    if check_space and shutil.disk_usage(destination.parent).free < total + 64 * 1024 * 1024:
        raise ValueError("临时磁盘空间不足，无法生成日常备份；请先腾出空间")
    update(status="packing", total_bytes=total, file_count=count, processed_bytes=0, completed_files=0)
    manifest = {
        "format": "qwenasr-daily-backup", "version": 1, "created_at": local_now(),
        "contains_secrets": True,
        "sources": [{"source": str(item.path), "archive": item.prefix, "excluded": list(item.excluded)} for item in sources],
        "files": [],
    }
    processed = completed = 0
    partial = destination.with_suffix(".partial")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            os.chmod(partial, 0o600)
            for path, name, stamp in entries:
                if _stamp(path) != stamp:
                    raise ValueError(f"备份期间文件发生变化，请重试：{path.name}")
                mode = stamp[2]
                if stat.S_ISDIR(mode):
                    archive.writestr(name.rstrip("/") + "/", b"")
                    continue
                info = (zipfile.ZipInfo(name) if stat.S_ISLNK(mode)
                        else zipfile.ZipInfo.from_file(path, arcname=name, strict_timestamps=False))
                info.create_system = 3
                info.external_attr = mode << 16
                digest = hashlib.sha256()
                if stat.S_ISLNK(mode):
                    payload = os.readlink(path).encode("utf-8")
                    archive.writestr(info, payload)
                    digest.update(payload)
                else:
                    # Already compressed recordings/PDFs need little CPU.
                    info.compress_type = (zipfile.ZIP_STORED if path.suffix.lower() in
                                          {".m4a", ".mp3", ".mp4", ".aac", ".pdf", ".zip", ".gz", ".png", ".jpg"}
                                          else zipfile.ZIP_DEFLATED)
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as source_file:
                        current = os.fstat(source_file.fileno())
                        if (current.st_dev, current.st_ino, current.st_mode, current.st_size, current.st_mtime_ns) != stamp:
                            raise ValueError(f"备份期间文件发生变化，请重试：{path.name}")
                        with archive.open(info, "w", force_zip64=True) as output:
                            while chunk := source_file.read(1024 * 1024):
                                output.write(chunk)
                                digest.update(chunk)
                                processed += len(chunk)
                                update(processed_bytes=processed)
                if _stamp(path) != stamp:
                    raise ValueError(f"备份期间文件发生变化，请重试：{path.name}")
                completed += 1
                manifest["files"].append({"path": name, "bytes": stamp[3], "sha256": digest.hexdigest(),
                                          "kind": "symlink" if stat.S_ISLNK(mode) else "file"})
                update(completed_files=completed)
            update(status="verifying")
            # Membership, timestamps and sizes must still match the starting
            # inventory. This detects Finder edits and background JSON writes.
            if inventory(sources) != entries:
                raise ValueError("备份期间资料库发生变化，未导出不完整的备份；请重试")
            archive.writestr("备份清单.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("恢复说明.txt", "QwenASR 日常资料备份\n\n此 ZIP 含 API 密钥、账号凭证和个人资料，请妥善保管。\n"
                            "资料库/：当前课程文件、日常录音、转写和 AI 笔记。\n配置/private/：当前配置、凭证与笔记目录。\n"
                            "配置/app-settings.json 与 配置/.env（如有）：资料库配置与环境配置。\n其他资料/（如有）：当前目录登记的外部录音、笔记。\n"
                            "恢复前停止 QwenASR，并保留目标位置的现有文件；按备份清单中的路径恢复各来源。\n"
                            "换电脑或换目录时，请更新 app-settings.json 的 storage_root、known_roots 和配置中的绝对路径。\n"
                            "可用 settings_recovery --root 已恢复资料库的绝对路径重新关联资料库。\n"
                            "模型、已安装的运行环境和当前服务进程信息不在本备份中；需另行准备。\n"
                            "历史任务、旧备份、转写源音频、音频分片、播放缓存和旧导出包不在日常备份中。\n"
                            "备份清单.json 记录每个文件的 SHA-256，可用于核对恢复结果。\n")
        partial.replace(destination)
        return {"archive_bytes": destination.stat().st_size, "file_count": count, "total_bytes": total}
    except BaseException:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


class BackupManager:
    def __init__(self):
        self.busy = False
        self.task = None
        self.directory = None
        self.job = None
        self.lock = threading.RLock()
        self.downloads = 0

    def public(self):
        with self.lock:
            if self.job is None:
                return None
            value = copy.deepcopy({key: value for key, value in self.job.items() if key != "path"})
        total = value.get("total_bytes", 0)
        value["percent"] = 100 if value["status"] in {"completed", "released"} else min(99, int(value.get("processed_bytes", 0) * 100 / total)) if total else None
        return value

    def update(self, **values):
        with self.lock:
            self.job.update(values)

    def start(self, sources):
        if self.busy:
            raise HTTPException(409, "日常备份正在打包，请等待完成")
        if self.downloads:
            raise HTTPException(409, "备份正在下载，请下载完成后再生成新备份")
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory = Path(tempfile.mkdtemp(prefix="qwenasr-full-backup-"))
        ident = uuid.uuid4().hex
        self.job = {"id": ident, "status": "scanning", "created_at": local_now(),
                    "filename": "QwenASR日常备份-" + local_now()[:19].replace(":", "-") + ".zip",
                    "file_count": 0, "completed_files": 0, "processed_bytes": 0, "total_bytes": 0}
        self.job["path"] = str(self.directory / self.job["filename"])
        self.busy = True
        self.task = asyncio.create_task(self._run(sources))
        return self.public()

    async def _run(self, sources):
        try:
            result = await asyncio.to_thread(build_backup, sources, Path(self.job["path"]), self.update)
            self.update(status="completed", completed_at=local_now(), **result,
                        download_url=f"/api/library/backup/{self.job['id']}/download")
        except Exception as exc:
            self.update(status="failed", error=f"日常备份失败：{exc}")
        finally:
            self.busy = False

    async def close(self):
        # Do not cancel a to_thread worker while it is still writing secrets.
        if self.task:
            await self.task
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)


def backup_router(manager, sources, require_idle):
    router = APIRouter()

    class BackupDownload(FileResponse):
        async def __call__(self, scope, receive, send):
            manager.downloads += 1
            try:
                await super().__call__(scope, receive, send)
            finally:
                manager.downloads -= 1
                if manager.downloads == 0:
                    if manager.job and manager.job.get('path') == str(self.path):
                        manager.update(status='released', download_url=None, temporary_zip_removed=True)
                    await asyncio.to_thread(Path(self.path).unlink, missing_ok=True)

    @router.get("/api/library/backup")
    async def latest_backup():
        return {"backup": manager.public()}

    @router.post("/api/library/backup", status_code=202)
    async def start_backup(request: Request):
        if request.headers.get("X-QwenASR-Client") != "library-backup":
            raise HTTPException(403, "请从“资料库与配置”导出日常备份")
        require_idle()
        try:
            return {"backup": manager.start(sources())}
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"无法导出日常备份：{exc}") from exc

    @router.get("/api/library/backup/{ident}")
    async def backup_status(ident: str):
        value = manager.public()
        if value is None or value["id"] != ident:
            raise HTTPException(404, "备份已过期，请重新导出")
        return {"backup": value}

    @router.get("/api/library/backup/{ident}/download")
    async def backup_download(ident: str):
        value = manager.public()
        if value is None or value["id"] != ident:
            raise HTTPException(404, "备份已过期，请重新导出")
        if value['status'] == 'released':
            raise HTTPException(404, "临时 ZIP 已清理，请重新导出")
        if value["status"] != "completed":
            raise HTTPException(409, "日常备份尚未完成")
        return BackupDownload(manager.job["path"], media_type="application/zip", filename=value["filename"],
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router
