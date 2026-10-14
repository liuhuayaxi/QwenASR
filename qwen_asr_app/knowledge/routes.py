"""FastAPI router for knowledge search, indexing jobs, status, and safe source file serving."""
from __future__ import annotations

import asyncio
import subprocess
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel


class IndexRequest(BaseModel):
    category: str


def validate_category(category: str, settings: Any) -> str:
    cat = (category or "").strip()
    if not cat:
        raise HTTPException(400, "课程类别不能为空")
    if ".." in cat or "/" in cat or "\\" in cat or "\x00" in cat:
        raise HTTPException(400, "课程类别包含非法路径字符")

    storage_root = Path(getattr(settings, "storage_root", Path.home() / "Documents" / "QwenASR资料库")).resolve()
    course_folder = (storage_root / cat).resolve()
    try:
        course_folder.relative_to(storage_root)
    except ValueError:
        raise HTTPException(400, "课程类别路径超出允许范围")

    categories = getattr(settings, "categories", None)
    if categories is not None and isinstance(categories, (list, set, tuple)):
        if cat not in categories:
            raise HTTPException(400, f"课程类别 '{cat}' 未在系统分类中注册")

    return cat


def knowledge_router(service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

    @router.get("/search")
    async def search(
        category: str = Query(..., description="课程类别"),
        q: str = Query(..., description="搜索词"),
        limit: int = Query(6, ge=1, le=50, description="返回数量"),
        lesson_id: str | None = Query(None, description="课次ID"),
        source_types: str | None = Query(None, description="逗号分隔的源类型过滤"),
        include_derived: bool = Query(False, description="是否包含AI衍生总结"),
    ) -> dict[str, Any]:
        cat = validate_category(category, service.settings)
        st_list = [s.strip() for s in source_types.split(",")] if source_types else None
        results = await asyncio.to_thread(
            service.search,
            category=cat,
            query=q,
            limit=limit,
            lesson_id=lesson_id,
            source_types=st_list,
            include_derived=include_derived,
        )
        status_info = await asyncio.to_thread(service.status, cat)
        return {
            "results": results,
            "status": status_info,
        }

    @router.post('/rerank')
    async def rerank(payload:dict):
        from .reranking import rank_candidates
        from .settings import knowledge_settings
        category=validate_category(payload.get('category',''),service.settings)
        query=str(payload.get('q') or '').strip()
        if not query or len(query)>4000:
            raise HTTPException(400,'请输入有效查询')
        if knowledge_settings.read()['reranker'] != 'qwen3-4b':
            raise HTTPException(409,'请先在知识检索设置中明确启用已准备的重排模型')
        if service.job_manager._active_job_id or any(r.metadata.get('status')=='active' for r in service.recording_store.list_records()):
            raise HTTPException(409,'课堂或索引正在使用本机模型，当前继续使用排名融合；课后空闲时可精排。')
        candidates=await asyncio.to_thread(service.search,category,query,8)
        try:
            ranked,diagnostic=await asyncio.to_thread(rank_candidates,query,candidates)
            return {'results':ranked,'strategy':'Qwen3 Reranker 4B','diagnostic':diagnostic}
        except (RuntimeError,TimeoutError,subprocess.TimeoutExpired) as exc:
            return {'results':candidates,'strategy':'排名融合','warning':str(exc)}

    @router.get("/status")
    async def get_status(
        category: str = Query(..., description="课程类别"),
    ) -> dict[str, Any]:
        cat = validate_category(category, service.settings)
        return await asyncio.to_thread(service.status, cat)

    @router.post("/index")
    async def trigger_index(payload: IndexRequest) -> dict[str, Any]:
        cat = validate_category(payload.category, service.settings)
        job_id = await asyncio.to_thread(service.schedule, cat)
        return {"job_id": job_id}

    @router.get("/jobs")
    async def list_jobs() -> dict[str, Any]:
        jobs = await asyncio.to_thread(service.jobs)
        return {"jobs": jobs}

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        ok = await asyncio.to_thread(service.cancel, job_id)
        if not ok:
            raise HTTPException(404, "任务未找到或已结束")
        return {"ok": True, "job_id": job_id}

    @router.get("/source/{source_id}")
    async def get_source_file(
        source_id: str,
        revision: str | None = Query(None, description="期望的修订版本"),
        page: int | None = Query(None, description="PDF页码"),
        start: float | None = Query(None, description="音频起始时间戳"),
        lesson_id: str | None = Query(None, description="课次ID"),
    ):
        source = await asyncio.to_thread(service.store.get_source, source_id)
        if source:
            await asyncio.to_thread(service._check_freshness_before_search, source['category'])
            source = await asyncio.to_thread(service.store.get_source, source_id)
        if source and source['status'] != 'active':
            raise HTTPException(409, '原件已更新或删除，请更新课程索引后重新打开来源')
        if not source:
            raise HTTPException(404, "找不到该知识源")

        if revision is not None:
            active_rev = source.get("active_revision")
            if active_rev and revision != active_rev:
                raise HTTPException(409, f"请求的修订版本已过时或不匹配 (当前: {active_rev}, 请求: {revision})")

        if source.get('source_type')=='course_terminology':
            return RedirectResponse(source['open_url'],status_code=307)

        # Audio transcript links jump audio reader rather than download session.json
        if source.get("source_type", "").startswith("recording_") or source.get("lesson_id"):
            lid = source.get("lesson_id") or (source_id.split("_")[1] if "_" in source_id else "")
            target_url = f"/?recording={lid}"
            if start is not None:
                target_url += f"&start={float(start)}"
            return RedirectResponse(target_url, status_code=307)

        file_path_str = source.get("file_path")
        if not file_path_str:
            raise HTTPException(404, "该源无对应物理文件")

        file_path = Path(file_path_str).resolve()
        if not file_path.is_file():
            raise HTTPException(404, "源文件不存在或已移出")

        # Security check: ensure path does not traverse outside storage_root or known roots
        storage_root = Path(getattr(service.settings, "storage_root", Path.home() / "Documents" / "QwenASR资料库")).resolve()
        known_roots = [
            Path(r).resolve() for r in getattr(service.settings, "known_roots", [])
        ]
        all_roots = [storage_root, *known_roots]

        is_safe = any(file_path.is_relative_to(root) for root in all_roots)
        if not is_safe:
            rec_store = service.recording_store
            if rec_store and hasattr(rec_store, "paths"):
                for p in rec_store.paths.values():
                    if file_path.is_relative_to(Path(p).resolve()):
                        is_safe = True
                        break

        if not is_safe:
            raise HTTPException(403, "非法的文件访问路径")

        headers = {}
        if page is not None:
            headers["X-Page-Number"] = str(page)
        if start is not None:
            headers["X-Audio-Start"] = str(start)

        media_type = None
        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            media_type = "application/pdf"
        elif suffix in (".aac", ".m4a"):
            media_type = "audio/aac"
        elif suffix == ".wav":
            media_type = "audio/wav"
        elif suffix == ".flac":
            media_type = "audio/flac"
        elif suffix in (".txt", ".md"):
            media_type = "text/plain; charset=utf-8"
        elif suffix == ".json":
            media_type = "application/json"

        return FileResponse(
            path=file_path,
            filename=source["title"] + (file_path.suffix if not source["title"].endswith(file_path.suffix) else ""),
            media_type=media_type,
            headers=headers,
            content_disposition_type='inline' if suffix in {'.pdf','.txt','.md'} else 'attachment',
        )

    return router
