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
        note = record.metadata.get('ai_note_summary')
        if not isinstance(note, dict): return {'enabled': True, 'sections': []}
        markdown = str(note.get('summary') or '')
        doc = section_pages.load(session_id)
        view = section_pages.public_view(doc, markdown) if doc else None
        if not view: return {'enabled': True, 'sections': []}
        by_source = {}
        resources = []
        for resource in doc.get('resources', []):
            manifest = section_pages.page_image_manifest(resource.get('viewer_sha256') or '')
            info = {'viewer_sha256': resource.get('viewer_sha256'), 'viewer_pages': manifest['pages'],
                    'pages_ready': True} if manifest else {}
            by_source[resource.get('source_id')] = info
            resources.append({'source_id': resource['source_id'], 'name': resource['name'],
                              'viewer_name': resource.get('viewer_name'), **info})
        for section in view.get('sections', []):
            for target in section.get('targets', []):
                target.update(by_source.get(target.get('source_id')) or {})
        return {'enabled': True, **view, 'resources': resources}

    @router.get('/api/section-page-links/{session_id}/pages/{source_id}/{name}')
    async def page_image(session_id: str, source_id: str, name: str):
        """A pre-rendered page image. Missing files fall back to the PDF viewer."""
        if not section_pages.enabled(): raise HTTPException(404, '页码跳转未开启')
        record_for(session_id)
        doc = section_pages.load(session_id) or {}
        resource = next((r for r in doc.get('resources', []) if r.get('source_id') == source_id), None)
        if resource is None: raise HTTPException(404, '这篇笔记没有关联该课件')
        path = section_pages.page_image_file(resource.get('viewer_sha256') or '', name)
        if path is None: raise HTTPException(404, '这一页还没有预渲染图片')
        media = 'application/json' if path.suffix == '.json' else 'image/png' if path.suffix == '.png' else 'image/webp'
        return FileResponse(path, media_type=media, content_disposition_type='inline',
                            headers={'Cache-Control': 'private, max-age=300'})

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
