"""Cheap change detection; one durable indexing actor owns all PDF processing."""
from __future__ import annotations

import asyncio
import hashlib
import os
import json
from pathlib import Path


class KnowledgeCoordinator:
    def __init__(self, service, interval=20):
        self.service=service
        self.interval=interval
        self.task=None
        self.stamps={}
        self.errors={}
        self.pending={}

    def fingerprint(self, category):
        folder=Path(self.service.settings.storage_root)/category
        digest=hashlib.sha256(str(folder.resolve()).encode())
        profile=(getattr(self.service.settings,'category_profiles',{}) or {}).get(category,{})
        digest.update(json.dumps(profile.get('terminology') or [],ensure_ascii=False).encode())
        for root,dirs,files in os.walk(folder,followlinks=False):
            dirs[:]=sorted(d for d in dirs if not d.startswith('.') and not (Path(root)/d).is_symlink())
            for name in sorted(files):
                p=Path(root)/name
                if name.startswith('.') or name.endswith(('.tmp','.part','.aac','.wav','.flac','.mp3','.m4a','.pcm')) or p.is_symlink():
                    continue
                try:
                    stat=p.stat()
                    digest.update(str(p.relative_to(folder)).encode())
                    digest.update(f'{stat.st_mtime_ns}:{stat.st_size}'.encode())
                except FileNotFoundError:
                    pass
        return digest.hexdigest()

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task=asyncio.create_task(self.run())

    async def run(self):
        while True:
            for category in list(self.service.settings.categories):
                try:
                    stamp=await asyncio.to_thread(self.fingerprint, category)
                    previous=self.stamps.get(category)
                    self.stamps[category]=stamp
                    # First boot only touches courses which already requested an index.
                    indexed=bool(self.service.store.get_course(category))
                    changed=(previous is not None and previous!=stamp) or (previous is None and indexed)
                    if changed:
                        self.pending[category]=stamp
                    if category in self.pending:
                        active=self.service.store.get_active_job_for_category(category)
                        if not active:
                            self.service.schedule(category)
                            self.pending.pop(category,None)
                    self.errors.pop(category,None)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.errors[category]=str(exc)
            await asyncio.sleep(self.interval)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
