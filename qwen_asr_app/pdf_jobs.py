"""Durable local PDF batches shared by the PDF tool and task center."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from threading import Event

from .conversion_control import ConversionStopped, check_stopped, conversion_worker
from .durable import atomic_json
from .paths import PRIVATE_ROOT, ROOT
from .pdf_ai import convert_pdf_to_ai, find_mineru_binary, validate_pdf_sources
from .mineru_settings import mineru_settings
from .remote_operations import saved_config, freeze, cancel, receipt, execution_lock, LocalComputeGuard, cancel_pending, recover_cancellation, download_result

ACTIVE = {"queued", "running", "stopping"}


class PDFJobs:
    def __init__(self, lock):
        self.lock = lock
        self.path = PRIVATE_ROOT / "tasks" / "pdf-jobs.json"
        self.jobs, self.tasks, self.controls = {}, {}, {}
        try:
            for job in json.loads(self.path.read_text(encoding="utf-8")):
                if job["status"] in ACTIVE:
                    job.update(status="interrupted", stage="服务重启，已完成文件保留，可继续")
                    for item in job["files"]:
                        if item["status"] in ACTIVE:
                            item.update(status="interrupted", stage="上次识别中断")
                self.jobs[job["id"]] = job
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            logging.exception("Cannot read saved PDF tasks")

    def save(self):
        atomic_json(self.path, list(self.jobs.values()))

    def snapshot(self, job):
        return {**job, "files": [dict(item) for item in job["files"]]}

    def start(self, paths, inference_config=None):
        if not isinstance(paths, list):
            raise ValueError("PDF 文件列表格式无效")
        sources = validate_pdf_sources(paths)
        active_sources = {item["source_path"] for job in self.jobs.values()
                          if job["status"] in ACTIVE for item in job["files"]}
        if any(str(source) in active_sources for source in sources):
            raise ValueError("所选文件已有识别任务，请在任务中心查看进度")
        from .inference_providers import inference_settings
        inference_config = inference_settings.for_task('mineru', inference_config)
        if inference_config['mineru_provider'] == 'local' and not find_mineru_binary():
            raise ValueError("未找到 MinerU，请先安装并下载模型")
        job = {"id": uuid.uuid4().hex, "status": "queued", "stage": "等待 MinerU 识别",
               "mineru_settings": mineru_settings.read(),
               "inference_settings": inference_config,
               "progress": 0, "created_at": time.time(), "updated_at": time.time(),
               "files": [{"source_path": str(source), "source_name": source.name,
                          "status": "pending", "stage": "等待处理", "progress": 0,
                          "output_path": "", "output_name": "", "error": ""} for source in sources]}
        if inference_config['mineru_provider'] != 'local':
            for item in job['files']:
                item['operation'] = freeze(Path(item['source_path']), inference_config, job['mineru_settings'], parent_id=job['id'])
        self.jobs[job["id"]] = job
        self.save()
        self.schedule(job)
        return self.snapshot(job)

    def schedule(self, job):
        ident = job["id"]
        stop = Event()
        self.controls[ident] = {"stop": stop, "action": "interrupted"}
        task = asyncio.create_task(self.run(job, stop))
        self.tasks[ident] = task

        def finished(done):
            if self.tasks.get(ident) is not done:
                return
            if job["status"] in ACTIVE:
                status = self.controls[ident]["action"]
                job.update(status=status, stage="识别已停止，可继续处理未完成文件", updated_at=time.time())
                self.save()
            self.tasks.pop(ident, None)
            self.controls.pop(ident, None)

        task.add_done_callback(finished)

    def receipt_path(self, job, index, item):
        if item.get('operation'):
            return Path(item['operation']['receipt_path'])
        base = PRIVATE_ROOT/'remote-operations'/job['id']/str(index)
        return (base/item['operation_attempt'] if item.get('operation_attempt') else base)/'receipt.json'

    def action(self, ident, action):
        job = self.jobs.get(ident)
        if job is None:
            raise ValueError("没有找到这项 PDF 任务")
        active = ident in self.tasks
        job['inference_settings'] = saved_config(job.get('inference_settings'),
            remote_reference=any(item.get('operation') for item in job['files']))
        if action in {'new_local', 'new_recognition'}:
            if active:
                raise ValueError('请先暂停旧任务，再创建新任务；旧远程操作可能仍在执行')
            return self.start([item['source_path'] for item in job['files']],
                {'mineru_provider': 'local'} if action == 'new_local' else job['inference_settings'])
        if action in {"pause", "cancel"}:
            target = "paused" if action == "pause" else "cancelled"
            if job["status"] == "completed":
                raise ValueError("这项任务已经完成")
            for index, item in enumerate(job['files']):
                if job['inference_settings']['mineru_provider'] != 'local':
                    cancel(self.receipt_path(job, index, item))
            if active:
                control = self.controls[ident]
                control.update(action=target)
                control["stop"].set()
                waiting = job["status"] == "queued"
                job.update(status="stopping", stage="正在停止识别，已完成文件保留")
                if waiting:
                    self.tasks[ident].cancel()
            else:
                job.update(status=target, stage="已暂停，可继续" if action == "pause" else "已取消，已完成文件保留")
        elif action in {"continue", "retry", "download"}:
            if active:
                raise ValueError("任务正在处理或停止，请稍后再试")
            pending = [item for item in job['files'] if item['status'] != 'completed' or action == 'download']
            if not pending:
                raise ValueError("所有文件已经完成")
            if job['inference_settings']['mineru_provider'] == 'local':
                validate_pdf_sources([item['source_path'] for item in pending])
            active_sources = {item["source_path"] for other in self.jobs.values()
                              if other["status"] in ACTIVE for item in other["files"]}
            if any(item["source_path"] in active_sources for item in pending):
                raise ValueError("文件已在另一项任务中识别")
            for item in pending:
                saved = receipt(self.receipt_path(job, job['files'].index(item), item))
                if action == 'download' and saved.get('state') not in {'completed', 'downloaded'}:
                    raise ValueError('远程结果尚未完成，不能仅恢复下载')
                if saved.get('state') in {'failed', 'canceled'}:
                    raise ValueError('旧操作已终止；请明确选择新识别，旧收据将保留')
                item.update(status="pending", progress=0, error="", stage="等待重新识别")
            job.update(status="queued", stage="等待 MinerU 识别", finished_at=0)
            # Continue/retry restores the saved operation and provider snapshot.
            # New recognition is created explicitly as a new task.

            job['recovery_action'] = action
            self.schedule(job)
        else:
            raise ValueError("无效的任务操作")
        job["updated_at"] = time.time()
        self.save()
        return self.snapshot(job)

    async def run(self, job, stop):
        try:
            provider = saved_config(job.get('inference_settings'), remote_reference=any(f.get('operation') for f in job['files']))
            async with execution_lock(self.lock, provider, 'mineru'):
                check_stopped(stop)
                job.update(status="running", started_at=time.time())
                self.save()
                loop = asyncio.get_running_loop()
                settings = job.get("mineru_settings")
                if settings is None:
                    settings = job["mineru_settings"] = mineru_settings.read()
                total = len(job["files"])
                for index, item in enumerate(job["files"]):
                    if item["status"] == "completed" and job.get("recovery_action") != "download":
                        continue
                    check_stopped(stop)
                    item.update(status="running", progress=None, stage="准备 MinerU")

                    def update(stage, percent, item=item, index=index):
                        if item["status"] != "running" or stop.is_set():
                            return
                        item.update(stage=stage, progress=None if percent is None else min(99, percent))
                        job.update(stage=f"{index + 1}/{total} · {item['source_name']} · {stage}",
                                   progress=None if any(f.get('progress') is None for f in job['files']) else round(sum(f['progress'] for f in job['files']) / total),
                                   updated_at=time.time())
                        self.save()

                    try:
                        path = self.receipt_path(job, index, item)
                        if provider['mineru_provider'] != 'local' and cancel_pending(path):
                            item['remote_operation'] = await recover_cancellation(path, provider)
                            item.update(status='paused', stage='取消状态已对账；新识别需新建任务')
                            self.save()
                            continue
                        if job.get('recovery_action') == 'download':
                            result = await download_result(path, provider, 'mineru', path.parent/'downloaded-output')
                            item.update(status='pending', stage='远程结果已下载，可继续进行本地打包', inference=result)
                            item['remote_operation'] = receipt(path)
                            self.save()
                            continue
                        inference_metadata = {}
                        output = await conversion_worker(convert_pdf_to_ai, Path(item.get('operation', {}).get('blob_path') or item['source_path']),
                            builder_script=ROOT / "scripts" / "build_ai_friendly_pdf.py",
                            python_executable=Path(sys.executable), stop=stop,
                            settings=settings, inference_metadata=inference_metadata,
                            inference_config=provider,
                            local_guard=LocalComputeGuard(self.lock, loop) if provider['mineru_provider'] != 'local' else None,
                            receipt_path=self.receipt_path(job, index, item),
                            progress=lambda stage, percent, callback=update: loop.call_soon_threadsafe(callback, stage, percent))
                        item.update(status="completed", stage="转换完成", progress=100,
                                    output_path=str(output), output_name=output.name, error="", mineru_settings=dict(settings), inference=inference_metadata)
                    except ConversionStopped:
                        raise
                    except Exception as exc:
                        item.update(status='pending' if getattr(exc, 'fallback_unsafe', False) else 'error', stage='等待恢复旧操作' if getattr(exc, 'fallback_unsafe', False) else '识别失败', error=str(exc))
                        item['remote_operation'] = receipt(self.receipt_path(job, index, item))
                    self.save()
                completed = sum(item["status"] == "completed" for item in job["files"])
                status = "completed" if completed == total else "completed_with_errors" if completed else "failed"
                if any(item['status'] in {'pending', 'paused'} for item in job['files']):
                    status = 'pending'
                job.update(status=status, stage=f"完成 {completed}/{total} 个文件", progress=100 if completed == total else None, finished_at=time.time())
        except (ConversionStopped, asyncio.CancelledError):
            target = self.controls[job["id"]]["action"]
            for item in job["files"]:
                if item["status"] in {"running", "pending"}:
                    item.update(status=target, stage="已停止；继续时重新识别此文件")
            job.update(status=target, stage="已暂停，已完成文件保留" if target == "paused" else "识别已停止，已完成文件保留")
        except Exception as exc:
            job.update(status="failed", stage=str(exc))
        finally:
            job["updated_at"] = time.time()
            self.save()
