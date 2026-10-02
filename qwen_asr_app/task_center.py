"""One task feed and action interface over existing durable task owners."""
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from .shared_library import discover_courses
from .course_tasks import course_tasks, course_action
from .library_layout import shared
from .pdf_ai import validate_pdf_sources, find_mineru_binary

ACTIVE = {"running", "queued", "waiting_retry", "stopping"}
ATTENTION = {"failed", "interrupted", "pending", "stale", "completed_with_errors"}


def actions_for(status):
    if status == "stopping":
        return []
    if status in ACTIVE:
        return ["pause", "cancel"]
    if status == "completed":
        return []
    return ["continue", "retry"] + ([] if status == "cancelled" else ["cancel"])


def task_center_router(ai_tasks, ai_action, courses, pdfs, uploads=None):
    router = APIRouter(prefix="/api/tasks")

    def snapshot():
        discover_courses(courses.store.settings)
        tasks = course_tasks()
        warnings = []
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
                if status == "error":
                    status = "failed"
                tasks.append({"id": f"course_pdf:{item['id']}", "type": "course_pdf", "family": "mineru",
                    "label": "课程资料识别", "title": item["name"], "category": category,
                    "source": "课程资料库", "asset_id": item["id"], "status": status,
                    "stage": "上次识别中断，可继续" if status == "interrupted" else state.get("stage", ""),
                    "progress": state.get("progress", 0), "error": state.get("error", "") or (state.get("stage", "") if status == "failed" else ""),
                    "updated_at": state.get("updated_at", ""), "actions": actions_for(status),
                    "warnings": state.get("warnings", []),
                    "result_url": "/course-files?" + urlencode({"category": category, "asset": item["id"]}),
                    "download_url": f"/api/course-files/{item['id']}/download?" + urlencode({"category": category, "variant": "pdf"}) if status == "completed" else ""})
        for job in pdfs.jobs.values():
            tasks.append({"id": f"pdf:{job['id']}", "type": "pdf", "family": "mineru",
                "job_id": job["id"], "title": job["files"][0]["source_name"] + (f" 等 {len(job['files'])} 个文件" if len(job["files"]) > 1 else ""),
                "label": "本地 PDF 识别", "category": "", "source": "本地 PDF 工具",
                "status": job["status"], "stage": job.get("stage", ""), "progress": job.get("progress", 0),
                "updated_at": job.get("updated_at") or job.get("finished_at") or job["created_at"],
                "actions": actions_for(job["status"]), "files": [
                    {**item, "download_url": f"/api/tasks/pdf/{job['id']}/files/{index}" if item["status"] == "completed" else ""}
                    for index, item in enumerate(job["files"])],
                "completed": sum(item["status"] == "completed" for item in job["files"]), "total": len(job["files"])})

        if uploads:
            for job in uploads.jobs.values():
                tasks.append({**uploads.snapshot(job), "id": "upload:" + job["id"], "job_id": job["id"],
                    "type": "upload", "family": "asr", "label": "上传录音转写", "source": "录音工作台",
                    "actions": actions_for(job["status"]),
                    "result_url": "/library?" + urlencode({"recording": job["recording_id"]}) if job["recording_id"] else "/library"})

        def timestamp(task):
            value = task.get("updated_at", "")
            try:
                return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()
            except (ValueError, TypeError):
                return 0

        order = {"running": 0, "stopping": 1, "queued": 2, "waiting_retry": 3}
        tasks.sort(key=lambda task: (order.get(task["status"], 4 if task["status"] in ATTENTION else 5), -timestamp(task)))
        return {"tasks": tasks, "warnings": warnings, "counts": {
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
        try:
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
    async def listing():
        return snapshot()

    @router.post("/action")
    async def action(request: Request, payload: dict = Body(...)):
        guard(request)
        return await perform(payload, snapshot()["tasks"])

    @router.post("/pdf")
    async def create_pdf(request: Request, payload: dict = Body(...)):
        guard(request)
        paths = payload.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20 or any(not isinstance(path, str) for path in paths):
            raise HTTPException(400, "请选择 1–20 个 PDF 文件")
        try:
            if not find_mineru_binary():
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
