"""Cheap change detection; one durable indexing actor owns all PDF processing."""
from __future__ import annotations

import asyncio
import hashlib
import os
import json
import time
from pathlib import Path

from ..background_wakeup import library_changes


class KnowledgeCoordinator:
    def __init__(self, service, interval=20, reconcile_interval=300):
        self.service=service
        self.interval=interval
        self.reconcile_interval=reconcile_interval
        self.task=None
        self.stamps={}
        self.errors={}
        self.pending={}
        self.dirty=set()
        self.checking=False

    def mark_dirty(self, paths=None):
        if paths is None:
            self.dirty.update(self.service.settings.categories)
            return
        root=Path(self.service.settings.storage_root)
        for raw in paths:
            path=Path(raw)
            try:
                parts=path.relative_to(root).parts
            except ValueError:
                continue
            if not parts:
                self.dirty.update(self.service.settings.categories)
            elif not path.name.endswith(('.tmp','.part','.aac','.wav','.flac','.mp3','.m4a','.pcm')):
                self.dirty.add(parts[0])

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
        self.mark_dirty()
        library_changes.callbacks.add(self.mark_dirty)
        self.task=asyncio.create_task(self.run())

    async def run(self):
        next_reconcile=0
        while True:
            version=library_changes.version
            now=time.monotonic()
            categories=set(self.service.settings.categories)
            if now>=next_reconcile:
                self.dirty.update(categories)
                next_reconcile=now+self.reconcile_interval
            check=(self.dirty | set(self.pending)) & categories
            self.dirty.difference_update(check)
            for category in sorted(check):
                try:
                    self.checking=True
                    try:
                        stamp=await asyncio.to_thread(self.fingerprint, category)
                    finally:
                        self.checking=False
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
            delay=max(0,next_reconcile-time.monotonic())
            if self.pending or self.errors:
                delay=min(delay,self.interval)
                self.dirty.update(self.errors)
            await library_changes.wait(version,delay)
            # Coalesce bursts from recording saves and external syncs.
            if self.dirty:
                await asyncio.sleep(min(1,self.interval))

    async def close(self):
        library_changes.callbacks.discard(self.mark_dirty)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
