"""Routes for section→courseware page links (feature-flagged)."""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import section_pages


def section_page_router(store, course_files):
    router = APIRouter()

    def record_for(ident):
        record = store.find(ident)
        if record is None: raise HTTPException(404, '没有找到录音')
        return record

    @router.get('/api/section-page-links/{session_id}')
    async def links(session_id: str):
        if not section_pages.enabled(): return {'enabled': False, 'sections': []}
        record = record_for(session_id)
        markdown = str((record.metadata.get('ai_note_summary') or {}).get('summary') or '')
        doc = section_pages.load(session_id)
        view = section_pages.public_view(doc, markdown) if doc else None
        if not view: return {'enabled': True, 'sections': []}
        return {'enabled': True, **view, 'resources': [{'source_id': r['source_id'], 'name': r['name'],
                'viewer_name': r.get('viewer_name')} for r in doc.get('resources', [])]}

    @router.get('/api/section-page-links/{session_id}/pdf/{source_id}')
    async def pdf(session_id: str, source_id: str):
        """The AI-friendly PDF of a linked courseware, served inline (Range-capable)."""
        if not section_pages.enabled(): raise HTTPException(404, '页码跳转未开启')
        record = record_for(session_id)
        doc = section_pages.load(session_id) or {}
        if not any(r['source_id'] == source_id for r in doc.get('resources', [])):
            raise HTTPException(404, '这篇笔记没有关联该课件')
        try:
            _, path = await asyncio.to_thread(course_files.store.file, str(record.metadata.get('category') or ''), source_id, 'pdf')
        except (ValueError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(Path(path), media_type='application/pdf', filename=Path(path).name,
                            content_disposition_type='inline', headers={'Cache-Control': 'private, max-age=300'})

    return router
