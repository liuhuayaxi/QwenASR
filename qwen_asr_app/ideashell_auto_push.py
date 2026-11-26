"""Server-owned, durable delivery of each generated note to IdeaShell."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from .durable import atomic_json


class AutoPushQueue:
    def __init__(self, path: Path, find, busy, deliver):
        self.path, self.find, self.busy, self.deliver = path, find, busy, deliver
        self.task = None
        self.wake = asyncio.Event()
        try:
            self.items = json.loads(path.read_text())
        except FileNotFoundError:
            self.items = []
        for item in self.items:
            if item['status'] == 'running':
                item['status'] = 'queued'

    def save(self):
        atomic_json(self.path, self.items)

    def enqueue(self, record, generation_id=''):
        if not str((record.metadata.get('ai_note_summary') or {}).get('summary') or '').strip():
            return
        # A generation retry reuses its receipt; a regeneration gets a new ID,
        # even if the resulting Markdown happens to be identical.
        generation_id = generation_id or uuid.uuid4().hex
        if any(i['recording_id'] == record.id and i['generation_id'] == generation_id for i in self.items):
            return
        item = dict(id=uuid.uuid4().hex, recording_id=record.id,
                               generation_id=generation_id, status='queued', attempts=0,
                               next_attempt=0, error='')
        self.items.append(item)
        self.save()
        record.metadata['ideashell_auto_push'] = {'status': 'queued', 'error': '', 'attempts': 0}
        record.save_metadata()
        self.wake.set()

    async def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def report(self, item, status, error=''):
        item.update(status=status, error=error, updated_at=time.time())
        self.save()
        record = self.find(item['recording_id'])
        if record is not None:
            record.metadata['ideashell_auto_push'] = {
                key: item[key] for key in ('status', 'error', 'attempts', 'generation_id', 'updated_at')
            }
            record.save_metadata()

    async def process(self, item):
        ident = item['recording_id']
        if self.busy(ident):
            return
        record = self.find(ident)
        if record is None:
            self.report(item, 'failed', '录音已移除，自动推送已停止')
            return
        if record.metadata.get('status') != 'completed':
            return
        # Never guess whether an interrupted create succeeded. Manual
        # reconciliation remains available through the existing push button.
        operation = (record.metadata.get('ideashell_sync') or {}).get('create_operation') or {}
        if operation.get('status') in {'sending', 'unknown'}:
            self.report(item, 'failed', '上次闪念贝壳新建结果未知，请通过手动推送按钮核对关联')
            return
        item['attempts'] += 1
        self.report(item, 'running')
        try:
            result = await self.deliver(ident, item['generation_id'])
            if not result.get('synced'):
                raise RuntimeError('闪念贝壳没有确认推送成功')
        except asyncio.CancelledError:
            item['status'] = 'queued'
            self.save()
            raise
        except Exception as exc:
            detail = str(getattr(exc, 'detail', exc))[:500]
            record = self.find(ident)
            operation = ((record.metadata.get('ideashell_sync') or {}).get('create_operation') or {}) if record else {}
            retry = item['attempts'] < 4 and operation.get('status') not in {'sending', 'unknown'}
            item['next_attempt'] = time.time() + (30, 120, 600)[min(item['attempts'] - 1, 2)]
            self.report(item, 'waiting_retry' if retry else 'failed', detail)
        else:
            self.report(item, 'completed')

    async def run(self):
        while True:
            self.wake.clear()
            for item in list(self.items):
                if item['status'] in {'queued', 'waiting_retry'} and item.get('next_attempt', 0) <= time.time():
                    await self.process(item)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
