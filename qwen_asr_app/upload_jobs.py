"""Durable uploaded-audio jobs; response subscribers never own execution."""
from __future__ import annotations
import asyncio
import copy
import json
import shutil
import time
import uuid
from collections import deque
from pathlib import Path
from .durable import atomic_json
from .paths import PRIVATE_ROOT

ACTIVE = {'queued', 'running', 'stopping'}


class UploadJobs:
    def __init__(self, runner, find_record, redact, remember, require_available=None):
        self.runner, self.find_record = runner, find_record
        self.redact, self.remember = redact, remember
        self.require_available = require_available or (lambda ident: None)
        self.root = PRIVATE_ROOT / 'uploads'
        self.path = self.root / 'jobs.json'
        self.jobs, self.tasks, self.events, self.notifications = {}, {}, {}, {}
        self.intent = {}
        self.sequence = {}
        self.lock = asyncio.Lock()
        try:
            for job in json.loads(self.path.read_text()):
                if job['status'] in ACTIVE:
                    job.update(status='interrupted', stage='服务曾关闭，可继续已保存的上传任务')
                self.jobs[job['id']] = job
        except FileNotFoundError:
            pass

    def save(self):
        atomic_json(self.path, list(self.jobs.values()))

    def snapshot(self, job):
        return {k: copy.deepcopy(v) for k, v in job.items() if k not in {'config', 'source_path'}}

    def record_for(self, job):
        # The process may have stopped after the archive was saved but before
        # its first event updated the job. Its preallocated ID bridges that gap.
        ident = job.get('recording_id') or job.get('config', {}).get('_upload_recording_id')
        record = self.find_record(ident) if ident else None
        if record is not None:
            job['recording_id'] = record.id
        return record

    def create(self, source, filename, config, record=None):
        if record:
            for job in self.jobs.values():
                identity = job.get('recording_id') or job.get('config', {}).get('_upload_recording_id')
                if identity != record.id:
                    continue
                if job['id'] in self.tasks:
                    return job
                self.require_available(record.id)
                self.remember(config)
                job.update(config=self.redact(config), recording_id=record.id,
                           title=record.metadata.get('title') or filename,
                           category=record.metadata.get('category') or '未分类',
                           status='queued', error='', stage='等待继续转写', updated_at=time.time())
                self.save()
                self.schedule(job, config)
                return job
            self.require_available(record.id)
        self.remember(config)
        ident = uuid.uuid4().hex
        config = copy.deepcopy(config)
        if not record:
            config["_upload_recording_id"] = "upload-" + ident
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / (ident + Path(filename).suffix[:12]) if source else None
        if source:
            shutil.move(str(source), destination)
        job = {'id': ident, 'filename': filename, 'title': config.get('title') or filename,
               'category': config.get('category') or '未分类', 'status': 'queued', 'progress': 0,
               'stage': '音频已上传，等待转写', 'created_at': time.time(), 'updated_at': time.time(),
               'source_path': str(destination or ''), 'recording_id': record.id if record else config['_upload_recording_id'],
               'config': self.redact(config), 'error': ''}
        self.jobs[ident] = job
        self.save()
        self.schedule(job, config)
        return job

    def schedule(self, job, config=None):
        ident = job['id']
        self.intent[ident] = 'interrupted'
        self.events[ident] = deque(maxlen=512)
        self.sequence[ident] = 0
        self.notifications[ident] = asyncio.Event()
        task = asyncio.create_task(self.run(job, config or job['config']))
        self.tasks[ident] = task
        def finished(done):
            if self.tasks.get(ident) is done:
                if job['status'] in ACTIVE:
                    job.update(status=self.intent[ident], stage='已停止，进度保留', updated_at=time.time())
                    self.save()
                self.tasks.pop(ident, None)
                self.notifications[ident].set()
        task.add_done_callback(finished)

    def emit(self, job, event):
        ident = job['id']
        self.sequence[ident] += 1
        self.events[ident].append((self.sequence[ident], event))
        self.notifications[ident].set()
        session = event.get('session')
        if session:
            job['recording_id'] = session['id']
        job.update(progress=max(job['progress'], event.get('progress') or 0),
                   stage=event.get('stage') or job['stage'], updated_at=time.time())
        if event['type'] == 'error':
            job.update(status='failed', error=event.get('message', '转写失败'))
        if event['type'] == 'complete':
            job.update(status='completed', progress=100, stage='转写已完成', error='')
        self.save()

    async def run(self, job, config):
        try:
            async with self.lock:
                job.update(status='running', error='')
                self.save()
                record = self.record_for(job)
                source = Path(job['source_path']) if job['source_path'] else None
                if record and record.metadata.get('status') == 'completed':
                    self.emit(job, {'type': 'complete', 'session': record.public(), 'progress': 100})
                else:
                    async for encoded in self.runner(source, job['filename'], config, existing_record=record, preserve_source=True):
                        self.emit(job, json.loads(encoded))
                if job['status'] == 'running':
                    job.update(status='failed', error='转写未返回完成结果')
                if job['status'] == 'completed' and source:
                    source.unlink(missing_ok=True)
        except asyncio.CancelledError:
            job.update(status=self.intent[job['id']], stage='已停止，已完成转写保留')
        except Exception as exc:
            job.update(status='failed', error=str(exc), stage='转写失败，可重试')
        finally:
            job['updated_at'] = time.time()
            self.save()

    async def action(self, ident, action):
        job = self.jobs.get(ident)
        if not job:
            raise ValueError('没有找到上传任务')
        task = self.tasks.get(ident)
        if action in {'pause', 'cancel'}:
            target = 'paused' if action == 'pause' else 'cancelled'
            if job['status'] == 'completed':
                raise ValueError('任务已经完成')
            self.intent[ident] = target
            if task and not task.done():
                job.update(status='stopping', stage='正在停止并保存进度')
                task.cancel()
            else:
                job.update(status=target, stage='已停止，进度保留')
        elif action in {'continue', 'retry'}:
            if task and not task.done():
                raise ValueError('任务正在运行或停止')
            if job['status'] == 'completed':
                raise ValueError('任务已经完成')
            record = self.record_for(job)
            if record:
                self.require_available(record.id)
            if not record and not Path(job['source_path']).is_file():
                raise ValueError('上传原件已不存在，请重新上传')
            job.update(status='queued', error='', stage='等待继续转写')
            self.schedule(job)
        else:
            raise ValueError('无效的上传任务操作')
        self.save()
        return self.snapshot(job)

    async def stream(self, ident):
        job = self.jobs[ident]
        # Existing clients receive live data. Reconnected clients can read the
        # durable recording; task completion is independent of this generator.
        sent = 0
        while ident in self.tasks:
            event = self.notifications[ident]
            event.clear()
            buffered = list(self.events[ident])
            for seq, item in buffered:
                if seq <= sent:
                    continue
                sent = seq
                yield (json.dumps(item, ensure_ascii=False) + '\n').encode()
            try:
                await asyncio.wait_for(event.wait(), 10)
            except asyncio.TimeoutError:
                yield (json.dumps({'type': 'progress', 'progress': job['progress'], 'stage': job['stage']}) + '\n').encode()
        if job['status'] == 'completed':
            record = self.find_record(job['recording_id'])
            result = ({'type': 'complete', 'progress': 100, 'session': record.public()} if record else
                      {'type': 'error', 'message': '转写已完成，但录音已被删除或移动'})
            yield (json.dumps(result, ensure_ascii=False) + '\n').encode()
        else:
            yield (json.dumps({'type': 'error', 'message': job.get('error') or job['stage']}, ensure_ascii=False) + '\n').encode()
