"""Chapter metadata operations. Note text and source files are never rewritten."""
import asyncio
from fastapi import APIRouter, Body, HTTPException

from .chapters import chapter_fields, labels_from_metadata, normalize_chapters, suggestion_for_record
from .storage import local_now


def save_chapter_fields(record, fields):
    previous = labels_from_metadata(record.metadata)
    record.metadata.update(fields)
    record.metadata['updated_at'] = local_now()
    sync = record.metadata.get('ideashell_sync')
    if isinstance(sync, dict) and sync.get('note_id') and previous != fields['chapters']:
        sync['status'] = 'local_changes'
    record.save_metadata()


def apply_manual_chapter(record, value):
    fields = chapter_fields(normalize_chapters(value), 'manual', [])
    save_chapter_fields(record, fields)
    return fields


def chapter_router(store, recording_operation):
    router = APIRouter()

    def find(ident):
        record = store.find(ident)
        if record is None:
            raise HTTPException(404, '没有找到这条课堂记录')
        return record

    @router.post('/api/recordings/{session_id}/chapter-suggestion')
    async def suggest(session_id: str):
        # A suggestion is a preview; only the explicit save changes a field.
        return await asyncio.to_thread(suggestion_for_record, find(session_id))

    @recording_operation
    async def detect_one(session_id: str):
        record = find(session_id)
        if record.metadata.get('status') != 'completed':
            return 'busy', record
        if (record.metadata.get('chapter_info') or {}).get('source') == 'manual':
            return 'unchanged', record
        fields = await asyncio.to_thread(suggestion_for_record, record)
        if not fields['chapters']:
            return ('unchanged' if labels_from_metadata(record.metadata) else 'unassigned'), record
        if labels_from_metadata(record.metadata) == fields['chapters'] and record.metadata.get('chapter_info') == fields['chapter_info']:
            return 'unchanged', record
        save_chapter_fields(record, fields)
        return 'updated', record

    @router.post('/api/library/chapters/detect')
    async def detect(payload: dict = Body(...)):
        category = str(payload.get('category') or '')
        result = dict(updated=0, unchanged=0, unassigned=0, busy=0, records=[], errors=[])
        ids = [item['id'] for item in store.iter_list_items()
               if item.get('has_note') and (not category or item.get('category') == category)]
        # Serial and cache-first: never run multiple PDF reads or conversions.
        for ident in ids:
            try:
                status, record = await detect_one(ident)
                result[status] += 1
                result['records'].append({'id': ident, 'chapter': record.metadata.get('chapter', ''),
                                          'chapters': labels_from_metadata(record.metadata)})
            except HTTPException as exc:
                if exc.status_code == 409:
                    result['busy'] += 1
                else:
                    result['errors'].append({'id': ident, 'error': str(exc.detail)})
            except (OSError, ValueError, TypeError) as exc:
                result['errors'].append({'id': ident, 'error': str(exc)[:300]})
        return result

    return router
