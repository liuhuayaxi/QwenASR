"""One task feed and action interface over existing durable task owners."""
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime
import json

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from .shared_library import discover_courses
from .course_tasks import course_tasks, course_action
from .library_layout import shared
from .pdf_ai import validate_pdf_sources, find_mineru_binary
from .remote_operations import receipt
from .durable import atomic_json
from .paths import PRIVATE_ROOT

ACTIVE = {"running", "queued", "waiting_retry", "stopping"}
ATTENTION = {"failed", "interrupted", "pending", "stale", "completed_with_errors"}


def inference_provenance(inference, operation=None):
    """Publish provenance fields without exposing private receipt contents."""
    inference = inference if isinstance(inference, dict) else {}
    operation = operation if isinstance(operation, dict) else {}
    return {
        "actual_provider": inference.get("actual_provider") or inference.get("provider"),
        "fallback_reason": inference.get("fallback_reason", ""),
        "remote_job_id": inference.get("job_id") or operation.get("job_id"),
        "remote_operation_state": operation.get("state") or inference.get("state"),
        "remote_operation_key": operation.get("key") or inference.get("operation_key"),
    }


def actions_for(status, remote=False):
    if status == "completed":
        return []
    if remote and status not in ACTIVE:
        return ['continue', 'retry', 'download', 'new_recognition', 'new_local', 'cancel']
    if status == "stopping":
        return []
    if status in ACTIVE:
        return ["pause", "cancel"]
    return ["continue", "retry"] + ([] if status == "cancelled" else ["cancel"])


def display_status(task):
    """A Mac poller waiting in the remote queue is not executing inference."""
    if task['status'] == 'running' and task.get('remote_operation_state') == 'queued':
        return 'queued'
    if task['status'] == 'running' and task.get('configured_provider') in {'remote_only', 'remote_preferred'}:
        unfinished = [f for f in task.get('files', []) if f.get('status') != 'completed']
        if unfinished and all(f.get('remote_operation_state') == 'queued' for f in unfinished):
            return 'queued'
    return task['status']


def history_generation(task):
    """An archived completion must not hide a later run of the same asset."""
    return json.dumps([task.get('id'), task.get('run_identity') or task.get('updated_at'), task.get('remote_operation_key'),
                       task.get('remote_job_id')], ensure_ascii=False, separators=(',', ':'))


class TaskHistory:
    """Archive feed entries only; task owners, files and receipts stay intact."""
    def __init__(self, path=None):
        self.path = Path(path) if path else PRIVATE_ROOT / 'tasks' / 'completed-history.json'

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'entries': {}, 'last_cleared': {}}
        if not isinstance(data, dict) or any(not isinstance(data.get(k), dict) for k in ('entries', 'last_cleared')):
            raise ValueError('已清理任务记录无法读取，请保留该文件并恢复备份')
        return data

    def visible(self, tasks):
        data = self.read()
        return [task for task in tasks if task['status'] != 'completed'
                or data['entries'].get(task['id']) != history_generation(task)], bool(data['last_cleared'])

    def clear(self, tasks, requested=None):
        data = self.read()
        wanted = None if requested is None else {item['id']: item['generation'] for item in requested}
        cleared = {task['id']: history_generation(task) for task in tasks if task['status'] == 'completed'
                   and (wanted is None or wanted.get(task['id']) == history_generation(task))
                   and data['entries'].get(task['id']) != history_generation(task)}
        if cleared:
            data['entries'].update(cleared)
            data['last_cleared'] = cleared
            atomic_json(self.path, data)
        return len(cleared)

    def restore(self):
        data = self.read()
        restored = 0
        for ident, generation in data['last_cleared'].items():
            if data['entries'].get(ident) == generation:
                del data['entries'][ident]
                restored += 1
        data['last_cleared'] = {}
        if restored or self.path.exists():
            atomic_json(self.path, data)
        return restored


def task_center_router(ai_tasks, ai_action, courses, pdfs, uploads=None, intelligence_tasks=None):
    router = APIRouter(prefix="/api/tasks")
    history = TaskHistory()

    def snapshot(include_archived=False):
        discover_courses(courses.store.settings)
        tasks = [dict(task) for task in course_tasks()]
        warnings = []
        if intelligence_tasks is not None:
            tasks.extend(intelligence_tasks.tasks())
        for task in ai_tasks(include_history=True):
            tasks.append({**task, "family": "ai", "title": task["recording_title"],
                          "source": "录音工作台", "stage": task.get("phase", ""),
                          "result_url": "/library?" + urlencode({"recording": task["recording_id"]})})
        for category in courses.store.settings.categories:
            try:
                items = courses.store.list(category)
            except (ValueError, OSError):
                warnings.append(f"暂时无法读取“{category}”的识别任务")
                continue
            for item in items:
                state = item.get("conversion", {})
                status = state.get("status", "none")
                # An externally supplied PDF has no local recognition task.
                if status == "none" or (state.get("external") and not state.get("engine")):
                    continue
                if status in ACTIVE and (category, item["id"]) not in courses.tasks:
                    status = "interrupted"
                if status in {'completed', 'cancelled'} and state.get('task_hidden'):
                    continue
                if status == "error":
                    status = "failed"
                remote = state.get('inference_settings', {}).get('mineru_provider', 'local') != 'local'
                operation = state.get('operation') or {}
                saved = receipt(operation.get('receipt_path')) if remote and operation else {}
                saved = saved or state.get('remote_operation', {})
                tasks.append({"id": f"course_pdf:{item['id']}", "type": "course_pdf", "family": "mineru",
                    "label": "课程资料识别", "title": item["name"], "category": category,
                    "source": "课程资料库", "asset_id": item["id"], "status": status,
                    "stage": "上次识别中断，可继续" if status == "interrupted" else state.get("stage", ""),
                    "progress": state.get("progress", 0), "error": state.get("error", "") or (state.get("stage", "") if status == "failed" else ""),
                    "progress_estimate": state.get('progress_estimate'),
                    "created_at": state.get("created_at", ""),
                    "updated_at": state.get("updated_at", ""), "actions": actions_for(status, remote),
                    "warnings": state.get("warnings", []),
                    "inference": state.get('inference', {}),
                    **inference_provenance(state.get('inference'), saved),
                    "configured_provider": state.get('inference_settings', {}).get('mineru_provider', 'local'),
                    "mineru_settings": state.get('mineru_settings', {}),
                    "result_url": "/course-files?" + urlencode({"category": category, "asset": item["id"]}),
                    "download_url": f"/api/course-files/{item['id']}/download?" + urlencode({"category": category, "variant": "pdf"}) if status == "completed" else ""})
        for job in list(pdfs.jobs.values()):
            files = []
            remote = job.get('inference_settings', {}).get('mineru_provider', 'local') != 'local'
            for index, item in enumerate(job['files']):
                saved = receipt(pdfs.receipt_path(job, index, item)) if remote else {}
                saved = saved or item.get('remote_operation', {})
                # Only these public fields reach the API; the durable receipt
                # remains private and is read afresh after restart/recovery.
                files.append({**item, **inference_provenance(item.get('inference'), saved),
                    'remote_operation': {k: saved[k] for k in ('job_id', 'state') if k in saved},
                    'download_url': f"/api/tasks/pdf/{job['id']}/files/{index}" if item['status'] == 'completed' else ''})
            tasks.append({"id": f"pdf:{job['id']}", "type": "pdf", "family": "mineru",
                "job_id": job["id"], "title": job["files"][0]["source_name"] + (f" 等 {len(job['files'])} 个文件" if len(job["files"]) > 1 else ""),
                "configured_provider": job.get('inference_settings', {}).get('mineru_provider', 'local'),
                "mineru_settings": job.get('mineru_settings', {}),
                "label": "本地 PDF 识别", "category": "", "source": "本地 PDF 工具",
                "status": job["status"], "stage": job.get("stage", ""), "progress": job.get("progress", 0),
                "progress_estimate": job.get('progress_estimate'),
                "created_at": job['created_at'],
                "updated_at": job.get("updated_at") or job.get("finished_at") or job["created_at"],
                "actions": actions_for(job["status"], remote), "files": files,
                "inference": files[0].get('inference', {}) if len(files) == 1 else {},
                **(inference_provenance(files[0].get('inference'), files[0]['remote_operation']) if len(files) == 1 else {}),
                **({'remote_operation_key': files[0]['remote_operation_key']} if len(files) == 1 else {}),
                "completed": sum(item["status"] == "completed" for item in job["files"]), "total": len(job["files"])})

        if uploads:
            for job in list(uploads.jobs.values()):
                tasks.append({**uploads.snapshot(job), "id": "upload:" + job["id"], "job_id": job["id"],
                    "type": "upload", "family": "asr", "label": "上传录音转写", "source": "录音工作台",
                    'inference': job.get('inference', {}),
                    **inference_provenance(job.get('inference'), uploads.remote_receipt(job)),
                    'configured_provider': job['config'].get('inference_settings', {}).get('asr_provider', 'unknown'),
                    "actions": actions_for(job["status"], job['config'].get('inference_settings', {}).get('asr_provider', 'local') != 'local'),
                    "result_url": "/library?" + urlencode({"recording": job["recording_id"]}) if job["recording_id"] else "/library"})

        def timestamp(task, field='updated_at'):
            value = task.get(field) or task.get('updated_at', '')
            try:
                return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()
            except (ValueError, TypeError):
                return 0

        for task in tasks:
            task['status'] = display_status(task)
            if task['status'] == 'completed':
                task['actions'] = []
            task['history_generation'] = history_generation(task)
        if include_archived:
            can_restore = False
        else:
            tasks, can_restore = history.visible(tasks)
        order = {"running": 0, "stopping": 1, "queued": 2, "waiting_retry": 3}
        tasks.sort(key=lambda task: (order.get(task["status"], 4 if task["status"] in ATTENTION else 5),
                                    timestamp(task, 'created_at') if task['status'] == 'queued' else -timestamp(task), task['id']))
        return {"tasks": tasks, "warnings": warnings, "history": {'can_restore': can_restore}, "counts": {
            "all": len(tasks), "active": sum(t["status"] in ACTIVE for t in tasks),
            "attention": sum(t["status"] in ATTENTION for t in tasks),
            "paused": sum(t["status"] == "paused" for t in tasks),
            "completed": sum(t["status"] == "completed" for t in tasks),
            "cancelled": sum(t["status"] == "cancelled" for t in tasks)}}

    def guard(request):
        if request.headers.get("X-QwenASR-Client") != "task-center":
            raise HTTPException(403, "请从任务中心操作")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "请从当前工作台操作")

    async def perform(payload, tasks):
        task = next((task for task in tasks if task["id"] == payload.get("id")), None)
        if task is None:
            raise HTTPException(404, "任务已更新或移除，请刷新")
        action = payload.get("action")
        if action not in task["actions"]:
            raise HTTPException(409, "任务状态已改变，当前不能执行这项操作")
        if action in {'new_local', 'new_recognition'} and payload.get('acknowledge_duplicate') is not True:
            raise HTTPException(409, '旧远端任务可能仍在执行；请确认重复计算风险后新建任务')
        try:
            if intelligence_tasks is not None and task['type'] in {'knowledge_index','classroom_note'}:
                return await intelligence_tasks.action(task,action)
            if task["type"] == "course_queue":
                return await course_action(action)
            if task["type"] == "upload":
                return await uploads.action(task["job_id"], action)
            if task["family"] == "ai":
                return await ai_action({**payload, "recording_id": task["recording_id"], "type": task["type"]})
            if task["type"] == "course_pdf":
                return await courses.conversion_action(task["category"], task["asset_id"], action)
            return pdfs.action(task["job_id"], action)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("")
    async def listing(request: Request, category: str = ''):
        from .library_query import conditional
        value = snapshot()
        if category:
            value['tasks'] = [task for task in value['tasks'] if task.get('category') == category]
        return conditional(value, request)

    @router.post("/action")
    async def action(request: Request, payload: dict = Body(...)):
        guard(request)
        return await perform(payload, snapshot()["tasks"])

    @router.post('/clear-completed')
    async def clear_completed(request: Request, payload: dict = Body(default={})):
        guard(request)
        items = payload.get('items')
        if items is not None and (not isinstance(items, list) or len(items) > 10000 or any(
                not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not isinstance(item.get('generation'), str) for item in items)):
            raise HTTPException(400, '已完成任务列表格式无效')
        try:
            cleared = history.clear(snapshot(include_archived=True)['tasks'], items)
            return {'cleared': cleared, **snapshot()}
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post('/restore-completed')
    async def restore_completed(request: Request):
        guard(request)
        try:
            restored = history.restore()
            return {'restored': restored, **snapshot()}
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/pdf")
    async def create_pdf(request: Request, payload: dict = Body(...)):
        guard(request)
        paths = payload.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20 or any(not isinstance(path, str) for path in paths):
            raise HTTPException(400, "请选择 1–20 个 PDF 文件")
        try:
            from .inference_providers import inference_settings
            if inference_settings.for_task('mineru')['mineru_provider'] == 'local' and not find_mineru_binary():
                raise ValueError("未找到 MinerU，请先安装并下载模型")
            discover_courses(courses.store.settings)
            root = courses.store.settings.storage_root.resolve()
            standalone, materials, seen = [], [], set()
            for raw in paths:
                path = Path(raw).expanduser()
                if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".pdf":
                    raise ValueError(f"不是有效的 PDF 绝对路径：{raw}")
                path = path.resolve()
                if path in seen:
                    continue
                seen.add(path)
                if shared(courses.store.settings) and path.is_relative_to(root):
                    relative = path.relative_to(root)
                    category = relative.parts[0]
                    item = next((item for item in courses.store.list(category)
                                 if item.get("relative_path") == Path(*relative.parts[1:]).as_posix()), None)
                    if item is None:
                        raise ValueError(f"请从课程资料库选择可识别的原件：{path.name}")
                    materials.append((category, item["id"]))
                else:
                    standalone.append(str(path))
            if standalone:
                validate_pdf_sources(standalone)
                busy = {item["source_path"] for job in pdfs.jobs.values()
                        if job["status"] in ACTIVE for item in job["files"]}
                if busy.intersection(standalone):
                    raise ValueError("所选文件已有任务，请在列表中查看")
            for category, ident in materials:
                await courses.start_conversion(category, ident)
            job = pdfs.start(standalone) if standalone else None
            return {"job": job, "course_files": len(materials)}
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/bulk-action")
    async def bulk_action(request: Request, payload: dict = Body(...)):
        guard(request)
        items = payload.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 200 or any(not isinstance(item, dict) for item in items):
            raise HTTPException(400, "每次请选择 1–200 项任务")
        results, seen = [], set()
        for item in items:
            ident = str(item.get("id", ""))
            if ident in seen:
                continue
            seen.add(ident)
            try:
                # Refresh capabilities between mutations; one action can finish another task.
                await perform({**item, "action": payload.get("action")}, snapshot()["tasks"])
                results.append({"id": ident, "ok": True})
            except HTTPException as exc:
                results.append({"id": ident, "ok": False, "error": str(exc.detail)})
        return {"results": results, "accepted": sum(item["ok"] for item in results), "requested": len(results)}

    @router.post("/retry-failed")
    async def retry_failed(request: Request):
        """Queue every currently visible failed/pending AI task in one click.

        The snapshot is taken once, then each operation is revalidated by
        ``perform`` so a task that finished while this request was running is
        reported rather than submitted a duplicate run.
        """
        guard(request)
        candidates = []
        for task in snapshot()["tasks"]:
            if task.get("status") not in ATTENTION:
                continue
            action = "retry" if "retry" in (task.get("actions") or []) else (
                "continue" if "continue" in (task.get("actions") or []) else ""
            )
            if action:
                candidates.append((task, action))
        if len(candidates) > 200:
            raise HTTPException(400, "失败任务超过 200 项，请按课程或类型分批重试")
        results = []
        for task, action in candidates:
            try:
                await perform({"id": task["id"], "action": action}, snapshot()["tasks"])
                results.append({"id": task["id"], "ok": True, "action": action})
            except HTTPException as exc:
                results.append({"id": task["id"], "ok": False, "action": action, "error": str(exc.detail)})
        return {
            "requested": len(results),
            "accepted": sum(bool(item["ok"]) for item in results),
            "failed": sum(not bool(item["ok"]) for item in results),
            "results": results,
        }

    @router.get("/pdf/{job_id}/files/{index}")
    async def download(job_id: str, index: int):
        job = pdfs.jobs.get(job_id)
        if job is None or index < 0 or index >= len(job["files"]):
            raise HTTPException(404, "没有找到识别结果")
        item = job["files"][index]
        path = Path(item.get("output_path") or "")
        if item["status"] != "completed" or not path.is_file():
            raise HTTPException(404, "结果文件已移动或删除")
        return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})

    return router
