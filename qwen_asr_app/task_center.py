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


def task_timestamp(task, field='updated_at'):
    value = task.get(field) or task.get('updated_at', '')
    try:
        return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def needs_attention(task):
    """Unstarted work and proven recovered receipts are not current errors."""
    return (not task.get('recovered') and not task.get('is_context')
            and task.get('status') in ATTENTION
            and (task.get('status') != 'pending' or bool(task.get('error'))))


def task_counts(tasks):
    return {"all": len(tasks), "active": sum(t['status'] in ACTIVE for t in tasks),
            "attention": sum(needs_attention(t) for t in tasks),
            "pending": sum(t['status'] == 'pending' and not t.get('recovered') for t in tasks),
            "review": sum(t.get('attention_kind') == 'review' and not t.get('recovered') for t in tasks),
            "paused": sum(t['status'] == 'paused' for t in tasks),
            "completed": sum(t['status'] == 'completed' for t in tasks),
            "cancelled": sum(t['status'] == 'cancelled' for t in tasks),
            "recovered": sum(bool(t.get('recovered')) for t in tasks)}


def group_tasks(tasks):
    """Project durable owners into lessons without changing their receipts.

    Queue entries are contextual progress for a lesson, not another failed
    lesson when its summary owner already exposes the same unfinished step.
    Action IDs always remain those of the original durable owner.
    """
    groups = {}
    expanded = []
    for task in tasks:
        if task.get('type') != 'course_queue' or not task.get('course_items'):
            expanded.append(dict(task))
            continue
        for item in task['course_items']:
            state = item.get('state') or 'queued'
            if state == 'error':
                state = 'failed'
            phases = item.get('phases') or []
            current = next((p for p in phases if p.get('state') in {'error', 'failed', 'interrupted'}), None)
            current = current or next((p for p in phases if p.get('state') in ACTIVE), None)
            stage = (current or {}).get('detail') or item.get('message') or task.get('stage') or ''
            expanded.append({**task, 'recording_id': item.get('recording_id') or '',
                             'course_item': item, 'title': item.get('title') or task['title'],
                             'category': item.get('course') or '', 'status': state,
                             'stage': stage, 'progress': (current or {}).get('percent',item.get('progress',0)),
                             'completed':sum(p.get('state')=='completed' for p in phases),
                             'total':len(phases), 'progress_unit':'阶段',
                             'next_action':('继续'+str((current or {}).get('label') or '未完成阶段')),
                             'warnings':[],
                             'error': stage if state in {'failed', 'interrupted'} else '',
                             'updated_at': item.get('updated_at') or task.get('updated_at') or '',
                             'display_status': state})
    for task in expanded:
        record_id = task.get('recording_id')
        if record_id:
            ident = 'recording:' + record_id
        elif task.get('type') == 'course_queue' and task.get('course_item'):
            ident = 'course-lesson:' + task['course_item']['id']
        elif task.get('type') == 'knowledge_index' and task.get('category'):
            ident = 'course:' + task['category']
        else:
            ident = 'task:' + task['id']
        group = groups.setdefault(ident, {'id': ident, 'title': task.get('title') or task.get('label') or '未命名任务',
            'category': task.get('category') or '', 'recording_id': record_id or '', 'tasks': []})
        group['tasks'].append(task)
    result = []
    for group in groups.values():
        tasks = group['tasks']
        owners = [t for t in tasks if t.get('type') != 'course_queue']
        for task in tasks:
            if task.get('type') == 'course_queue' and owners:
                # Keep its controls and full phase evidence in details, while
                # current AI owners supply the actionable state of that step.
                unfinished = [p.get('key') for p in task['course_item'].get('phases', [])
                              if p.get('state') != 'completed']
                covered = {'summary': {'summary'}, 'review': {'summary'}, 'verification': {'refinement','summary'}, 'correction': {'refinement'}, 'asr': {'remote_asr', 'upload'}}
                if unfinished and all(any(o.get('type') in covered.get(key, set()) for o in owners) for key in unfinished):
                    task['is_context'] = True
        history = [t for t in tasks if t.get('recovered') or t['status'] in {'completed', 'cancelled'}]
        current = [t for t in tasks if t not in history and not t.get('is_context')]
        errors = [t for t in current if needs_attention(t)]
        running = [t for t in current if t['status'] in ACTIVE]
        pending = [t for t in current if t['status'] == 'pending']
        paused = [t for t in current if t['status'] == 'paused']
        if errors:
            status = 'failed'
            primary = max(errors, key=task_timestamp)
        elif running:
            status = next(state for state in ('running', 'stopping', 'waiting_retry', 'queued')
                          if any(t['status'] == state for t in running))
            primary = max(running, key=task_timestamp)
        elif pending:
            status, primary = 'pending', max(pending, key=task_timestamp)
        elif paused:
            status, primary = 'paused', max(paused, key=task_timestamp)
        else:
            status = 'cancelled' if tasks and all(t['status'] == 'cancelled' for t in tasks) else 'completed'
            primary = max(tasks, key=task_timestamp)
        group.update(status=status, current_stage=primary.get('stage') or primary.get('phase') or primary.get('label') or '',
            updated_at=primary.get('updated_at') or '', result_url=next((t['result_url'] for t in owners if t.get('result_url')), primary.get('result_url') or ''),
            current_tasks=current, history=history, context_tasks=[t for t in tasks if t.get('is_context')],
            errors=[{'id': t['id'], 'label': t.get('label') or t.get('type') or '', 'error': t.get('error') or t.get('stage') or '上次处理未完成', 'status': t['status']} for t in errors],
            remaining=[{'id': t['id'], 'label': t.get('label') or t.get('type') or '', 'status': t['status'], 'stage': t.get('stage') or t.get('phase') or ''} for t in current],
            recovered_count=sum(bool(t.get('recovered')) for t in tasks),
            active_count=len(running),
            review_count=sum(t.get('attention_kind') == 'review' for t in current))
        result.append(group)
    order = {'failed': 0, 'running': 1, 'stopping': 1, 'waiting_retry': 2, 'queued': 3,
             'pending': 4, 'paused': 5, 'completed': 6, 'cancelled': 7}
    result.sort(key=lambda group: (order.get(group['status'], 9), -task_timestamp(group), group['id']))
    counts = {'all': len(result), 'attention': sum(bool(g['errors']) for g in result),
              'active': sum(bool(g['active_count']) for g in result),
              'recovered': sum(bool(g['recovered_count']) for g in result),
              'review': sum(bool(g['review_count']) for g in result)}
    for status in ('pending', 'paused', 'completed', 'cancelled'):
        counts[status] = sum(g['status'] == status for g in result)
    return result, counts


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

    def snapshot(include_archived=False, category_filter=''):
        discover_courses(courses.store.settings)
        tasks = [dict(task) for task in course_tasks()]
        warnings = []
        if intelligence_tasks is not None:
            tasks.extend(dict(task) for task in intelligence_tasks.tasks())
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

        for task in tasks:
            task['status'] = display_status(task)
            task.setdefault('display_status', task['status'])
            if task['status'] == 'completed':
                task['actions'] = []
            task['history_generation'] = history_generation(task)
        if include_archived:
            can_restore = False
        else:
            tasks, can_restore = history.visible(tasks)
        if category_filter:
            filtered = []
            for task in tasks:
                if task.get('type') == 'course_queue':
                    items = [item for item in task.get('course_items', []) if item.get('course') == category_filter]
                    if items:
                        filtered.append({**task, 'course_items': items})
                elif task.get('category') == category_filter:
                    filtered.append(task)
            tasks = filtered
        order = {"running": 0, "stopping": 1, "queued": 2, "waiting_retry": 3}
        tasks.sort(key=lambda task: (order.get(task["status"], 4 if needs_attention(task) else 5),
                                    task_timestamp(task, 'created_at') if task['status'] == 'queued' else -task_timestamp(task), task['id']))
        groups, group_counts = group_tasks(tasks)
        return {"tasks": tasks, "groups": groups, "group_counts": group_counts,
                "warnings": warnings, "history": {'can_restore': can_restore}, "counts": task_counts(tasks)}

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
                result = await ai_action({**payload, "recording_id": task["recording_id"], "type": task["type"]})
                if isinstance(result, dict) and result.get('accepted') is False:
                    raise HTTPException(409, result.get('reason') or '任务尚未接受，请稍后再试')
                return result
            if task["type"] == "course_pdf":
                return await courses.conversion_action(task["category"], task["asset_id"], action)
            return pdfs.action(task["job_id"], action)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("")
    async def listing(request: Request, category: str = ''):
        from .library_query import conditional
        value = snapshot(category_filter=category)
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
        """Retry only current failures, preserving reviews and recovered history.

        Group projections remove duplicate queue context, while owner IDs and
        capabilities remain authoritative for each action. Recheck failure
        eligibility between mutations as well as the owner's capability.
        """
        guard(request)

        def retry_targets(view):
            current_errors = {task['id'] for group in view['groups'] for task in group['current_tasks']
                              if needs_attention(task)}
            targets = {}
            for task in view['tasks']:
                if task['id'] not in current_errors or not needs_attention(task):
                    continue
                allowed = task.get('actions') or []
                action = 'retry' if 'retry' in allowed else 'continue' if 'continue' in allowed else ''
                if action:
                    targets[task['id']] = action
            return targets

        candidates = retry_targets(snapshot())
        if len(candidates) > 200:
            raise HTTPException(400, "失败任务超过 200 项，请按课程或类型分批重试")
        results = []
        for ident, action in candidates.items():
            try:
                current = snapshot()
                if retry_targets(current).get(ident) != action:
                    raise HTTPException(409, '任务状态已更新，当前无需重试')
                await perform({"id": ident, "action": action}, current['tasks'])
                results.append({"id": ident, "ok": True, "action": action})
            except HTTPException as exc:
                results.append({"id": ident, "ok": False, "action": action, "error": str(exc.detail)})
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
