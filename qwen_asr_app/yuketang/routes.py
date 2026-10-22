"""HTTP API for the 雨课堂题库 panel on the course page."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .service import YuketangService


def yuketang_router(service: YuketangService, guard: Callable[[Request], None], categories: Callable[[], list[str]]) -> APIRouter:
    router = APIRouter(prefix="/api/yuketang")

    def known(category: str) -> str:
        category = str(category or "").strip()
        if category not in categories():
            raise HTTPException(404, "课程不存在，请刷新后重试")
        return category

    @router.get("/status")
    def status(category: str | None = Query(None)) -> dict[str, Any]:
        return service.status(known(category) if category else None)

    @router.post("/bind")
    def bind(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        guard(request)
        category = known(payload.get("category"))
        try:
            entry = service.store.bind(category, payload.get("classroom_id"),
                                       university_id=payload.get("university_id"), name=str(payload.get("name") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"binding": entry, **service.status(category)}

    @router.post("/unbind")
    def unbind(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        guard(request)
        category = known(payload.get("category"))
        try:
            removed = service.store.unbind(category, payload.get("classroom_id"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"removed": removed, **service.status(category)}

    def start(action):
        try:
            return action()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/login")
    def login(request: Request) -> dict[str, Any]:
        guard(request)
        return {"job": start(service.start_login)}

    @router.post("/sync")
    def sync(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        guard(request)
        category = known(payload.get("category")) if payload.get("category") else None
        return {"job": start(lambda: service.start_sync(category, refresh=bool(payload.get("refresh")),
                                                        transcribe=bool(payload.get("transcribe"))))}

    @router.post("/transcribe")
    def transcribe(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        guard(request)
        category = known(payload.get("category")) if payload.get("category") else None
        return {"job": start(lambda: service.start_transcribe(category))}

    @router.post("/stop")
    def stop(request: Request) -> dict[str, Any]:
        guard(request)
        service.stop()
        return {"stopping": service.running}

    @router.get("/questions")
    def questions(category: str = Query(...), q: str = Query(""), source: str = Query(""),
                  limit: int = Query(200)) -> dict[str, Any]:
        category = known(category)
        items = service.store.search(category, q, source, limit)
        return {"category": category, "questions": items, "counts": service.store.counts(service.store.course_questions(category))}

    @router.get("/image/{classroom_id}/{path:path}")
    def image(classroom_id: str, path: str) -> FileResponse:
        try:
            return FileResponse(service.store.image_path(classroom_id, path), headers={"Cache-Control": "private, max-age=86400"})
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "图片不存在") from exc

    return router
