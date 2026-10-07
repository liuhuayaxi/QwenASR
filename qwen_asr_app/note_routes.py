"""Recording search, note history and seekable audio routes."""
import asyncio
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse
from .library_search import LibrarySearch
from . import note_history
from .audio import make_playback_audio
from .course_replay import replay_target


def note_router(store, recording_operation):
    router = APIRouter()
    search_index = LibrarySearch()
    playback_locks = {}

    def record_for(ident):
        record = store.find(ident)
        if record is None:
            raise HTTPException(404, '没有找到录音')
        return record

    @router.get('/api/library/search')
    async def search(q: str = '', category: str = '', offset: int = 0, limit: int = 30):
        if len(q) > 200 or offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(400, '搜索参数无效')
        return search_index.search(store.list_records(), q, category, offset, limit)

    @router.get('/api/recordings/{session_id}/versions')
    async def versions(session_id: str):
        return {'versions': note_history.versions(record_for(session_id))}

    @router.get('/api/recordings/{session_id}/replay-target')
    async def course_replay_target(session_id: str, seconds: float = 0):
        record = record_for(session_id)
        try:
            return replay_target(record, seconds)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/api/recordings/{session_id}/versions/{version_id}')
    async def version(session_id: str, version_id: str):
        record = record_for(session_id)
        try:
            note = note_history.read_version(record, version_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        current = str((record.metadata.get('ai_note_summary') or {}).get('summary') or '')
        diff = await asyncio.to_thread(note_history.difference, note['summary'], current)
        return {'version': note, 'diff': diff, 'current_summary': current}

    @router.post('/api/recordings/{session_id}/versions/{version_id}/restore')
    @recording_operation
    async def restore(session_id: str, version_id: str, payload: dict = Body(...)):
        record = record_for(session_id)
        current = str((record.metadata.get('ai_note_summary') or {}).get('summary') or '')
        if payload.get('expected_summary') != current:
            raise HTTPException(409, '笔记已经更新，请重新比较版本')
        try:
            previous = note_history.read_version(record, version_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        record.metadata['ai_note_summary'] = previous
        summary = record.update_note_summary(previous['summary'], source='restored')
        return {'summary': summary, 'session': record.public()}

    @router.get('/api/recordings/{session_id}/playback')
    @recording_operation
    async def playback(session_id: str):
        record = record_for(session_id)
        if record.metadata.get('status') != 'completed':
            raise HTTPException(409, '结束并保存录音后可以回听')
        source = record.audio_path
        if not source.is_file():
            raise HTTPException(404, '录音文件已不存在')
        lock = playback_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            output = record.directory / '.playback.m4a'
            if not output.exists() or output.stat().st_mtime_ns < source.stat().st_mtime_ns:
                worker = asyncio.create_task(asyncio.to_thread(make_playback_audio, source, output))
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(worker)
                        break
                    except asyncio.CancelledError:
                        if worker.cancelled():
                            raise
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
        return FileResponse(output, media_type='audio/mp4', headers={'Cache-Control': 'private, max-age=3600'})

    return router
