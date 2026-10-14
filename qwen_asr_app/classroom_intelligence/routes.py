"""FastAPI router for classroom intelligence scribbles, related materials, and settings."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response

from ..storage import safe_component
from .export import export_notes_markdown
from .service import ClassroomIntelligenceService, configure_intelligence
from .settings import get_intelligence_settings, save_intelligence_settings, intelligence_secret_store
from .store import ClassroomNoteStore, notes_file_path, read_notes_doc

logger = logging.getLogger(__name__)


def classroom_intelligence_router(
    store: Any,
    knowledge: Any = None,
    preferences: Any = None,
    secrets: Any = None,
    live_sessions: Any = None,
) -> APIRouter:
    router = APIRouter()
    live_sessions_ref = live_sessions if live_sessions is not None else {}
    live_sessions = live_sessions_ref
    service = ClassroomIntelligenceService(
        store=store,
        knowledge=knowledge,
        preferences=preferences,
        secrets=secrets,
        live_sessions=live_sessions_ref,
    )
    router.intelligence_service = service
    configure_intelligence(service)

    note_to_recording: dict[str, str] = {}

    def get_record(session_id: str) -> Any:
        session = live_sessions.get(session_id)
        record = getattr(session, 'recording', None) or (store.find(session_id) if hasattr(store, 'find') else None)
        if record is None:
            raise HTTPException(404, '没有找到录音')
        return record

    def find_record_for_note(note_id: str, session_id: str | None = None) -> tuple[Any, str]:
        if session_id:
            rec = get_record(session_id)
            note_to_recording[note_id] = str(getattr(rec, 'id', session_id))
            return rec, str(getattr(rec, 'id', session_id))

        if note_id in note_to_recording:
            rec_id = note_to_recording[note_id]
            rec = get_record(rec_id)
            return rec, rec_id

        # Scan store recordings to locate the note
        list_fn = getattr(store, 'list_records', None) or getattr(store, 'list_recordings', None)
        if list_fn and callable(list_fn):
            for r in list_fn():
                doc = read_notes_doc(r)
                if any(n.get('id') == note_id for n in doc.get('notes', [])):
                    rid = str(getattr(r, 'id', ''))
                    note_to_recording[note_id] = rid
                    return r, rid

        raise HTTPException(404, '未找到指定速记所属的录音，请提供 recording_id')

    # ==================== Settings Endpoints ====================

    @router.get('/api/intelligence/settings')
    async def get_settings():
        try:
            return get_intelligence_settings(secrets=intelligence_secret_store)
        except ValueError as exc:
            raise HTTPException(500, f'读取课堂智能笔记设置失败，已保留原配置：{exc}') from exc
        except Exception as exc:
            raise HTTPException(500, f'读取课堂智能笔记设置失败: {exc}') from exc

    @router.put('/api/intelligence/settings')
    async def put_settings(payload: dict = Body(...)):
        try:
            return save_intelligence_settings(payload, secrets=intelligence_secret_store)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f'保存课堂智能笔记设置失败: {exc}') from exc

    # ==================== Scribbles Endpoints ====================

    @router.get('/api/recordings/{session_id}/scribbles')
    async def get_scribbles(session_id: str):
        record = get_record(session_id)
        note_store = ClassroomNoteStore(record, live_sessions.get(session_id))
        notes, context = await note_store.list_notes()
        for n in notes:
            note_to_recording[n['id']] = session_id
            for owner in [n,n.get('suggestion') or {}]:
                evidence=owner.get('evidence') or []
                material=[e for e in evidence if e.get('type')=='material']
                if material and knowledge is not None:
                    import asyncio
                    checked=await asyncio.to_thread(knowledge.validate_evidence,material)
                    by_id={e.get('citation_id'):e for e in checked}
                    owner['evidence']=[by_id.get(e.get('citation_id'),e) for e in evidence]
        return {'notes': notes, 'context': context}

    @router.post('/api/recordings/{session_id}/scribbles')
    async def create_scribble(session_id: str, payload: dict = Body(...)):
        record = get_record(session_id)
        note_store = ClassroomNoteStore(record, live_sessions.get(session_id))

        text = payload.get('text')
        anchor = payload.get('anchor', 'current')
        start = payload.get('start')
        end = payload.get('end')
        request_id = payload.get('request_id')

        try:
            note = await note_store.add_note(
                text=text,
                anchor=anchor,
                start=start,
                end=end,
                request_id=request_id,
            )
            note_to_recording[note['id']] = session_id
            return {'note': note}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # PATCH note handler
    async def _handle_patch_note(note_id: str, payload: dict, session_id: str | None = None):
        record, _ = find_record_for_note(note_id, session_id)
        note_store = ClassroomNoteStore(record, live_sessions.get(session_id))

        if 'expected_revision' not in payload:
            raise HTTPException(400, '缺少 expected_revision 参数')
        try:
            expected_revision = int(payload['expected_revision'])
        except (ValueError, TypeError):
            raise HTTPException(400, 'expected_revision 必须为整数')

        try:
            note = await note_store.patch_note(
                note_id=note_id,
                expected_revision=expected_revision,
                text=payload.get('text'),
                content=payload.get('content'),
                locked=payload.get('locked'),
                anchor=payload.get('anchor'),
                start=payload.get('start'),
                end=payload.get('end'),
            )
            return {'note': note}
        except KeyError:
            raise HTTPException(404, '速记不存在')
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.patch('/api/recordings/{session_id}/scribbles/{note_id}')
    async def patch_scribble_scoped(session_id: str, note_id: str, payload: dict = Body(...)):
        return await _handle_patch_note(note_id, payload, session_id)

    @router.patch('/api/scribbles/{note_id}')
    @router.patch('/scribbles/{note_id}')
    async def patch_scribble(note_id: str, payload: dict = Body(...), session_id: str | None = Query(None)):
        return await _handle_patch_note(note_id, payload, session_id)

    # POST enhance handler
    async def _handle_enhance_note(note_id: str, payload: dict | None = None, session_id: str | None = None):
        record, _ = find_record_for_note(note_id, session_id)
        note_store = ClassroomNoteStore(record, live_sessions.get(session_id))
        note = await note_store.get_note(note_id)
        if not note:
            raise HTTPException(404, '速记不存在')

        if note.get('locked'):
            raise HTTPException(409, '速记已锁定，无法进行 AI 增强')

        debounce = float((payload or {}).get('debounce_seconds') or 0.0)
        await service.enqueue_enhancement(record, note_id, debounce_seconds=debounce, force=True)

        # Return updated note status immediately
        updated = await note_store.get_note(note_id)
        return {'note': updated or note}

    @router.post('/api/recordings/{session_id}/scribbles/{note_id}/enhance')
    async def enhance_scribble_scoped(session_id: str, note_id: str, payload: dict = Body(None)):
        return await _handle_enhance_note(note_id, payload, session_id)

    @router.post('/api/scribbles/{note_id}/enhance')
    @router.post('/scribbles/{note_id}/enhance')
    async def enhance_scribble(note_id: str, payload: dict = Body(None), session_id: str | None = Query(None)):
        return await _handle_enhance_note(note_id, payload, session_id)

    # POST action handler
    async def _handle_note_action(note_id: str, payload: dict, session_id: str | None = None):
        action = payload.get('action')
        if not action or not isinstance(action, str):
            raise HTTPException(400, '缺少 action 操作参数')

        record, _ = find_record_for_note(note_id, session_id)
        note_store = ClassroomNoteStore(record, live_sessions.get(session_id))

        if action == 'regenerate':
            return await _handle_enhance_note(note_id, payload, session_id)

        if action == 'cancel':
            await service.cancel_task(note_id)

        try:
            note = await note_store.apply_action(
                note_id=note_id,
                action=action,
                expected_revision=payload.get('expected_revision'),
                suggestion_id=payload.get('suggestion_id'),
                knowledge=knowledge,
            )
            return {'note': note}
        except KeyError:
            raise HTTPException(404, '速记不存在')
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.post('/api/recordings/{session_id}/scribbles/{note_id}/action')
    async def action_scribble_scoped(session_id: str, note_id: str, payload: dict = Body(...)):
        return await _handle_note_action(note_id, payload, session_id)

    @router.post('/api/scribbles/{note_id}/action')
    @router.post('/scribbles/{note_id}/action')
    async def action_scribble(note_id: str, payload: dict = Body(...), session_id: str | None = Query(None)):
        return await _handle_note_action(note_id, payload, session_id)

    # ==================== Related Intelligence ====================

    @router.post('/api/recordings/{session_id}/intelligence/context')
    async def set_source_context(session_id: str, payload: dict = Body(...)):
        from .store import get_recording_lock,save_notes_doc
        record=get_record(session_id)
        category=record.metadata.get('category','')
        source_id=str(payload.get('source_id') or '')
        page=payload.get('page')
        heading=str(payload.get('heading') or '').strip()[:200]
        if page is not None and (isinstance(page,bool) or not isinstance(page,int) or page<1 or page>10000):
            raise HTTPException(400,'页码必须为正整数')
        if page and not source_id:raise HTTPException(400,'请先选择当前课件再指定页码')
        hint={'heading':heading} if heading else {}
        if source_id:
            if knowledge is None or not hasattr(knowledge,'source_context'):
                raise HTTPException(409,'请先更新本课程知识索引')
            import asyncio
            refs=await asyncio.to_thread(knowledge.source_context,category,source_id,page)
            if not refs:raise HTTPException(409,'该资料页不可用或已更新，请重新选择')
            hint.update(source_id=source_id,page=page,revision=refs[0]['revision'],title=refs[0]['title'])
        async with await get_recording_lock(session_id):
            doc=read_notes_doc(record);doc['source_hint']=hint;save_notes_doc(record,doc)
        return {'source_hint':hint}

    @router.get('/api/recordings/{session_id}/intelligence/related')
    @router.get('/recordings/{session_id}/intelligence/related')
    @router.get('/api/intelligence/related')
    @router.get('/intelligence/related')
    async def get_related_materials(
        session_id: str | None = None,
        source_id: str | None = Query(None),
        page: int | None = Query(None),
        heading: str | None = Query(None),
        recording_id: str | None = Query(None),
        category: str | None = Query(None),
        q: str | None = Query(None),
    ):
        rec_id = session_id or recording_id
        record = get_record(rec_id) if rec_id else None
        return await service.get_related(
            record=record,
            source_id=source_id,
            page=page,
            heading=heading,
            category=category,
            query=q,
        )

    # ==================== Export Endpoints ====================

    def _export_response(record: Any) -> Response:
        from urllib.parse import quote
        markdown_text = export_notes_markdown(record,'http://127.0.0.1:'+os.getenv('QWEN_ASR_PORT','8765'))
        ascii_id = safe_component(record.id, 'notes')
        raw_title = str(record.metadata.get('title') or record.id)
        encoded_filename = quote(f"{raw_title}-notes.md")
        return Response(
            content=markdown_text.encode('utf-8'),
            media_type='text/markdown; charset=utf-8',
            headers={
                'Content-Disposition': f'attachment; filename="{ascii_id}-notes.md"; filename*=UTF-8\'\'{encoded_filename}'
            },
        )

    @router.get('/api/recordings/{session_id}/scribbles/export')
    async def export_recording_scribbles(session_id: str):
        record = get_record(session_id)
        return _export_response(record)

    @router.get('/api/scribbles/export')
    @router.get('/scribbles/export')
    async def export_scribbles(
        session_id: str | None = Query(None),
        recording_id: str | None = Query(None),
        id: str | None = Query(None),
    ):
        target = session_id or recording_id or id
        if not target:
            raise HTTPException(400, '必须提供 recording_id 或 session_id')
        record = get_record(target)
        return _export_response(record)

    return router
