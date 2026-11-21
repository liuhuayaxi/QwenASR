"""Local VGA extraction jobs feeding the ordinary course material library."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import tempfile
from threading import Event
import time
import uuid

from fastapi import Body, HTTPException, Request

from .durable import atomic_json
from .paths import PRIVATE_ROOT
from .storage import safe_component


ACTIVE = {'queued', 'running', 'stopping'}


def validate_video(path):
    import av

    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                raise ValueError('所选文件没有视频轨道，请选择 VGA 录屏视频')
    except av.error.FFmpegError as exc:
        raise ValueError('无法读取视频，请检查文件是否完整且为有效视频') from exc


class VGAJobs:
    def __init__(self, store, lock, tasks, root=None):
        self.store, self.lock, self.tasks = store, lock, tasks
        self.root = Path(root) if root is not None else PRIVATE_ROOT / 'vga-slides'
        self.path = self.root / 'jobs.json'
        self.jobs, self.controls = {}, {}
        try:
            for job in json.loads(self.path.read_text(encoding='utf-8')):
                if job['status'] in ACTIVE:
                    job.update(status='interrupted', stage='上次提取中断，请重新开始')
                self.jobs[job['id']] = job
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            logging.exception('Cannot read saved VGA tasks')

    def save(self):
        atomic_json(self.path, list(self.jobs.values()))

    def snapshot(self, job):
        return dict(job)

    def routes(self, router, guard):
        @router.post('/from-vga')
        async def start(request: Request, payload: dict = Body(...)):
            guard(request)
            category = str(payload.get('category') or '')
            raw = str(payload.get('video_path') or '').strip()
            try:
                if category not in self.store.settings.categories:
                    raise ValueError('请先选择有效课程')
                if not raw:
                    raise ValueError('请输入本机 VGA 录屏视频的完整路径')
                for key in ('filter_enabled', 'keep_homework', 'emphasis_enabled'):
                    if key in payload and not isinstance(payload[key], bool):
                        raise ValueError('过滤、作业讲解和重点标记选项必须为布尔值')
                if 'audio_path' in payload and not isinstance(payload['audio_path'], str):
                    raise ValueError('录音路径必须为文本')
                path = Path(raw).expanduser().resolve()
                if not path.is_file():
                    raise ValueError('视频文件不存在或不是文件，请检查本机路径')
                await asyncio.to_thread(validate_video, path)
                async with self.lock:
                    if any(job['status'] in ACTIVE for job in self.jobs.values()):
                        raise HTTPException(409, '已有 VGA 提取任务，请等待完成或取消后再开始')
                    name = safe_component(str(payload.get('display_name') or path.stem), '录屏')
                    job = {'id': uuid.uuid4().hex, 'category': category, 'video_path': str(path),
                           'name': name + '(VGA课件).pdf', 'status': 'queued', 'progress': 0,
                           'stage': '等待提取 VGA 课件', 'created_at': time.time(),
                           'filter_enabled': payload.get('filter_enabled', True),
                           'keep_homework': payload.get('keep_homework', True),
                           'emphasis_enabled': payload.get('emphasis_enabled', True),
                           'audio_path': payload.get('audio_path', '').strip()}
                    self.jobs[job['id']] = job
                    stop = Event()
                    self.controls[job['id']] = stop
                    self.save()
                    key = (category, 'vga:' + job['id'])
                    task = asyncio.create_task(self.run(job, stop))
                    self.tasks[key] = task
                    def finished(done):
                        self.tasks.pop(key, None)
                        self.controls.pop(job['id'], None)
                    task.add_done_callback(finished)
                return self.snapshot(job)
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.get('/from-vga')
        async def latest(category: str):
            jobs = [job for job in self.jobs.values() if job['category'] == category]
            return {'job': self.snapshot(max(jobs, key=lambda job: job['created_at'])) if jobs else None}

        @router.get('/from-vga/{job_id}')
        async def status(job_id: str):
            return self.snapshot(self.get(job_id))

        @router.post('/from-vga/{job_id}/cancel')
        async def cancel(request: Request, job_id: str):
            guard(request)
            job = self.get(job_id)
            if job['status'] in ACTIVE and job_id in self.controls:
                self.controls[job_id].set()
                job.update(status='stopping', stage='正在取消提取，未导入的结果将丢弃')
                self.save()
            return self.snapshot(job)

    def get(self, ident):
        if ident not in self.jobs:
            raise HTTPException(404, 'VGA 提取任务不存在')
        return self.jobs[ident]

    async def run(self, job, stop):
        loop = asyncio.get_running_loop()

        def update(elapsed, total):
            if stop.is_set() or job['status'] not in ACTIVE:
                return
            percent = min(94, int(elapsed / total * 94)) if total else 0
            stage = f'正在提取最终稳定画面（已处理 {int(elapsed)} 秒）'
            job.update(status='running', progress=percent, stage=stage)

        def extract(output):
            from .vga_slides import extract_slides
            from .vga_filter import filter_slides
            slides = extract_slides(job['video_path'], cancel=stop.is_set,
                deduplicate=not job.get('filter_enabled', True),
                progress=lambda elapsed, total: loop.call_soon_threadsafe(update, elapsed, total))
            if not slides:
                raise ValueError('未找到稳定课件画面，请检查录屏内容或使用更长的视频')
            result = filter_slides(slides, enabled=job.get('filter_enabled', True),
                                   keep_homework=job.get('keep_homework', True), cancel=stop.is_set)
            return result

        worker = None
        try:
            with tempfile.TemporaryDirectory(prefix='qwen-vga-job-') as raw:
                output = Path(raw) / 'slides.pdf'
                job.update(status='running', stage='正在读取 VGA 录屏')
                self.save()
                worker = asyncio.create_task(asyncio.to_thread(extract, output))
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    stop.set()
                    await asyncio.gather(worker, return_exceptions=True)
                    raise
                from .vga_emphasis import apply_emphasis
                from .vga_filter import write_filtered_pdf
                def audio_progress(stage):
                    job.update(stage='正在根据录音标记重点：' + stage)
                job.update(progress=95, stage='正在匹配录音重点')
                await apply_emphasis(result, job['video_path'], audio=job.get('audio_path'),
                                     enabled=job.get('emphasis_enabled', True), category=job['category'],
                                     cancel=stop.is_set, progress=audio_progress)
                job.update(warnings=result.warnings, recording_id=result.recording_id)
                worker = asyncio.create_task(asyncio.to_thread(write_filtered_pdf, result, output, cancel=stop.is_set))
                try:
                    manifest = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    stop.set()
                    await asyncio.gather(worker, return_exceptions=True)
                    raise
                async with self.lock:
                    if stop.is_set():
                        job.update(status='cancelled', stage='已取消，未导入课件')
                    else:
                        job.update(progress=97, stage='正在保存课件到课程资料库')
                        item, duplicate = self.store.import_file(job['category'], output, job['name'], '课件')
                        job.update(status='completed', progress=100, stage='课件已保存，可用于笔记与复习',
                                   asset_id=item['id'], duplicate=duplicate, pages=len(manifest['pages']))
                        try:
                            atomic_json(self.root / (item['id'] + '.json'), manifest)
                        except OSError:
                            logging.exception('Cannot save VGA timestamp manifest')
        except asyncio.CancelledError:
            stop.set()
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
            job.update(status='interrupted', stage='提取已中断，请重新开始')
            raise
        except Exception as exc:
            if stop.is_set():
                job.update(status='cancelled', stage='已取消，未导入课件')
            else:
                logging.exception('VGA extraction failed')
                job.update(status='failed', stage='VGA 提取失败', error=str(exc))
        finally:
            self.save()
