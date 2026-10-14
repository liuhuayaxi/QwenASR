"""Atomic, serialized per-recording storage for classroom scribbles and notes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..durable import atomic_json
from ..note_content import transcript_blocks
from ..storage import local_now

_recording_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def get_recording_lock(record_id: str) -> asyncio.Lock:
    async with _locks_guard:
        if record_id not in _recording_locks:
            _recording_locks[record_id] = asyncio.Lock()
        return _recording_locks[record_id]


def notes_file_path(record: Any) -> Path:
    return Path(record.directory) / 'classroom-notes.json'


def read_notes_doc(record: Any) -> dict[str, Any]:
    path = notes_file_path(record)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('notes'), list):
            raise ValueError('课堂笔记文件损坏')
        for note in data['notes']:
            if 'manual_edit' not in note:
                history=note.get('history') or []
                note['manual_edit']=bool(history and history[-1].get('source')=='manual')
        return data
    except FileNotFoundError:
        return {'version': 1, 'recording_id': str(getattr(record, 'id', '')), 'notes': []}


def save_notes_doc(record: Any, doc: dict[str, Any]) -> None:
    path = notes_file_path(record)
    doc['updated_at'] = local_now()
    atomic_json(path, doc)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def calculate_anchor_range(
    record: Any,
    anchor: str,
    start: float | None = None,
    end: float | None = None,
    live_session: Any = None,
) -> tuple[float, float]:
    """Calculate sample-clock start and end times based on anchor type and recording state."""
    duration = float(getattr(record, 'duration', 0) or 0)
    for value in (start,end):
        if value is not None and (not math.isfinite(float(value)) or float(value)<0 or float(value)>duration+1):
            raise ValueError('时间锚点应在本堂已录音范围内')
    if start is not None and end is not None and float(end)<float(start):
        raise ValueError('结束时间不能早于开始时间')
    blocks = transcript_blocks(record)

    if start is not None and end is not None:
        return float(start), max(float(start), float(end))

    if anchor == 'manual':
        s = float(start) if start is not None else 0.0
        e = float(end) if end is not None else duration
        return s, max(s, e)

    if anchor == 'current':
        if start is not None:
            return float(start), float(end) if end is not None else max(float(start), duration)
        if blocks:
            latest = blocks[-1]
            return float(latest['start']), max(float(latest['end']), duration)
        return max(0.0, duration - 30.0), duration

    if anchor == 'previous':
        if start is not None and end is not None:
            return float(start), float(end)
        if len(blocks) >= 2:
            prev = blocks[-2]
            return float(prev['start']), float(prev['end'])
        if len(blocks) == 1:
            return float(blocks[0]['start']), float(blocks[0]['end'])
        return max(0.0, duration - 60.0), max(0.0, duration - 30.0)

    # Fallback
    return max(0.0, duration - 30.0), duration


def history_snapshot(note, at, source):
    return {key:copy.deepcopy(note.get(key)) for key in ('revision','content','evidence','accepted_claims','manual_edit','anchor','start','end','last_model','accepted_evidence_fingerprint')} | {'updated_at':at,'source':source}


class ClassroomNoteStore:
    """Manages CRUD and optimistic concurrency for classroom notes."""

    def __init__(self, record: Any, live_session: Any = None) -> None:
        self.record = record
        self.live_session = live_session
        self.record_id = str(getattr(record, 'id', ''))

    async def list_notes(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        lock = await get_recording_lock(self.record_id)
        async with lock:
            doc = read_notes_doc(self.record)
            duration = float(getattr(self.record, 'duration', 0) or 0)
            blocks = transcript_blocks(self.record)
            transcribed_until = max((b['end'] for b in blocks), default=0)
            context_info = {
                'recording_id': self.record_id,
                'title': self.record.metadata.get('title', '') if getattr(self.record, 'metadata', None) else '',
                'category': self.record.metadata.get('category', '') if getattr(self.record, 'metadata', None) else '',
                'duration': duration,
                'transcribed_until': transcribed_until,
                'note_count': len(doc['notes']),
                'source_hint': doc.get('source_hint') or {},
            }
            return copy.deepcopy(doc['notes']), context_info

    async def recover_interrupted_notes(self, running_task_ids: set[str] | None = None) -> int:
        """Called on startup or retry recovery to recover notes left pending across process restart."""
        running = running_task_ids or set()
        lock = await get_recording_lock(self.record_id)
        async with lock:
            doc = read_notes_doc(self.record)
            modified = False
            recovered_count = 0
            for note in doc['notes']:
                if note.get('status') == 'pending' and note.get('id') not in running:
                    note['status'] = 'failed'
                    note['error'] = '上次生成被服务重启中断，可以重试'
                    job = note.setdefault('job', {})
                    retry_count = int(job.get('retry_count', 0)) + 1
                    job['retry_count'] = retry_count
                    job['status'] = 'waiting_retry'
                    job['retryable'] = True
                    backoff = min(300.0, 5.0 * (2 ** min(10, max(0, retry_count - 1))))
                    job['backoff_seconds'] = backoff
                    job['next_retry_at'] = time.time() + backoff
                    job['last_error'] = note['error']
                    modified = True
                    recovered_count += 1
            if modified:
                save_notes_doc(self.record, doc)
            return recovered_count

    async def add_note(
        self,
        text: str,
        anchor: str = 'current',
        start: float | None = None,
        end: float | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Immediate atomic save of raw scribble before any background enqueue."""
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
            raise ValueError('速记内容必须为 1–8000 个字符')
        text = text.strip()
        anchor = str(anchor or 'current').lower()
        if anchor not in {'current', 'previous', 'manual'}:
            raise ValueError("锚点必须为 'current', 'previous' 或 'manual'")

        note_id = request_id if (request_id and re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id)) else f"note-{uuid.uuid4().hex[:12]}"
        s, e = calculate_anchor_range(self.record, anchor, start, end, self.live_session)

        lock = await get_recording_lock(self.record_id)
        async with lock:
            doc = read_notes_doc(self.record)
            existing = next((n for n in doc['notes'] if n['id'] == note_id), None)
            if existing:
                if (
                    existing['raw'] == text
                    and existing.get('anchor') == anchor
                    and abs(float(existing.get('start', 0.0)) - s) < 1e-4
                    and abs(float(existing.get('end', 0.0)) - e) < 1e-4
                ):
                    return copy.deepcopy(existing)
                raise ValueError('请求编号已用于另一条不同参数的速记')

            now = local_now()
            note = {
                'id': note_id,
                'raw': text,
                'text': text,
                'content': text,
                'revision': 1,
                'start': s,
                'end': e,
                'anchor': anchor,
                'locked': False,
                'status': 'idle',
                'created_at': now,
                'updated_at': now,
                'suggestion': None,
                'evidence': [],
                'history': [],
            }
            doc['notes'].append(note)
            save_notes_doc(self.record, doc)
            return copy.deepcopy(note)

    async def get_note(self, note_id: str) -> dict[str, Any] | None:
        lock = await get_recording_lock(self.record_id)
        async with lock:
            doc = read_notes_doc(self.record)
            note = next((n for n in doc['notes'] if n['id'] == note_id), None)
            return copy.deepcopy(note) if note else None

    async def patch_note(
        self,
        note_id: str,
        expected_revision: int,
        text: str | None = None,
        content: str | None = None,
        locked: bool | None = None,
        anchor: str | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> dict[str, Any]:
        """Optimistic concurrency: reject if revision differs; protect manual edits."""
        lock = await get_recording_lock(self.record_id)
        async with lock:
            doc = read_notes_doc(self.record)
            note = next((n for n in doc['notes'] if n['id'] == note_id), None)
            if not note:
                raise KeyError('速记不存在')

            if note.get('locked') and locked is not False:
                raise ValueError('速记已锁定，不可修改')

            current_rev = int(note.get('revision', 1))
            if int(expected_revision) != current_rev:
                raise ValueError(f'版本冲突：当前版本为 {current_rev}，您编辑的版本为 {expected_revision}，请刷新后重试')

            now = local_now()
            modified = False
            new_content = content if content is not None else text
            if new_content is not None and str(new_content).strip() != note.get('content'):
                # Save previous content to history for undo
                note.setdefault('history', []).append(history_snapshot(note, now, 'manual'))
                note['manual_edit'] = True
                note['content'] = str(new_content).strip()
                note['text'] = note['content']
                modified = True
                # Mark any existing suggestion as stale suggestion only; never overwrite manual edit
                if note.get('suggestion'):
                    note['suggestion']['stale'] = True

            if locked is not None and bool(locked) != bool(note.get('locked')):
                note['locked'] = bool(locked)
                modified = True

            if anchor is not None:
                anchor = str(anchor).lower()
                if anchor not in {'current', 'previous', 'manual'}:
                    raise ValueError('无效的锚点类型')
                if anchor != note.get('anchor'):
                    note['anchor'] = anchor
                    modified = True
                    if note.get('suggestion'):
                        note['suggestion']['stale'] = True

            if start is not None and abs(float(start) - float(note.get('start', 0.0))) > 1e-4:
                note['start'] = float(start)
                modified = True
                if note.get('suggestion'):
                    note['suggestion']['stale'] = True

            if end is not None and abs(float(end) - float(note.get('end', 0.0))) > 1e-4:
                note['end'] = float(end)
                modified = True
                if note.get('suggestion'):
                    note['suggestion']['stale'] = True

            if modified:
                note['revision'] = current_rev + 1
                note['updated_at'] = now
                save_notes_doc(self.record, doc)
            return copy.deepcopy(note)

    async def apply_action(
        self,
        note_id: str,
        action: str,
        expected_revision: int | None = None,
        suggestion_id: str | None = None,
        knowledge: Any = None,
    ) -> dict[str, Any]:
        """Apply actions: accept, reject, undo, cancel, lock, unlock, regenerate."""
        action = str(action).lower().strip()
        lock = await get_recording_lock(self.record_id)
        async with lock:
            doc = read_notes_doc(self.record)
            note = next((n for n in doc['notes'] if n['id'] == note_id), None)
            if not note:
                raise KeyError('速记不存在')

            now = local_now()
            current_rev = int(note.get('revision', 1))

            if action == 'lock':
                note['locked'] = True
                note['updated_at'] = now
                save_notes_doc(self.record, doc)
                return copy.deepcopy(note)

            if action == 'unlock':
                note['locked'] = False
                note['updated_at'] = now
                save_notes_doc(self.record, doc)
                return copy.deepcopy(note)

            if action == 'accept':
                if note.get('locked'):
                    raise ValueError('速记已锁定，无法接受建议')
                if expected_revision is not None and int(expected_revision) != current_rev:
                    raise ValueError(f'版本冲突：当前版本为 {current_rev}，提供版本为 {expected_revision}')

                suggestion = note.get('suggestion')
                if not suggestion or not isinstance(suggestion, dict):
                    raise ValueError('没有可接受的 AI 建议')

                if suggestion_id and str(suggestion.get('id')) != str(suggestion_id):
                    raise ValueError('建议编号已过期')

                if suggestion.get('stale') or int(suggestion.get('input_revision', 0)) != current_rev:
                    raise ValueError('速记在生成期间被手动修改，该建议已过期，无法直接覆盖用户编辑')

                # Source freshness check: material evidence
                if knowledge is not None and hasattr(knowledge, 'validate_evidence'):
                    ev_list = suggestion.get('evidence', [])
                    mat_list = [e for e in ev_list if e.get('type') == 'material']
                    if mat_list:
                        validated = knowledge.validate_evidence(mat_list)
                        if any(item.get('stale') for item in validated):
                            raise ValueError('引用的教材资料版本已发生变化，请重新生成')

                # Source freshness check: classroom transcript
                current_blocks = transcript_blocks(self.record)
                curr_block_texts = ' '.join(b.get('text', '') for b in current_blocks)
                claims = suggestion.get('claims') or {}
                cls_claims = claims.get('classroom') or []
                for c in cls_claims:
                    quote = c.get('quote')
                    if quote and quote not in curr_block_texts:
                        raise ValueError('引用的课堂转写在生成后已发生变更，请重新生成')

                # Source freshness check: verify classroom chunk content_hash if present
                current_hashes = {
                    hashlib.sha256(b.get('text', '').encode('utf-8')).hexdigest()
                    for b in current_blocks
                }
                for ev in suggestion.get('evidence', []):
                    if ev.get('type') == 'classroom' and ev.get('content_hash'):
                        if ev['content_hash'] not in current_hashes and ev.get('text') not in curr_block_texts:
                            raise ValueError('引用的课堂转写分段内容已变更，请重新生成')

                # User edit protection: capture current content in history before applying
                note.setdefault('history', []).append(history_snapshot(note, now, 'before_ai_accept'))
                protected=bool(note.get('manual_edit'))
                new_claims = [c for kind in ('classroom','material') for c in (suggestion.get('claims') or {}).get(kind,[])]
                note['accepted_claims'] = (note.get('accepted_claims') or [])+new_claims if protected else new_claims
                if protected and note['content'].strip()!=suggestion['content'].strip():
                    note['content'] = note['content'].rstrip()+'\n\n## 已采纳的课堂与资料补充\n\n'+suggestion['content']
                else:
                    note['content'] = suggestion['content']
                note['manual_edit'] = protected
                note['text'] = note['content']
                note['revision'] = current_rev + 1
                note['status'] = 'enhanced'
                evidence=(note.get('evidence') or [])+suggestion.get('evidence',[]) if protected else suggestion.get('evidence',[])
                note['evidence']=list({(e.get('citation_id'),e.get('revision')):e for e in evidence}.values())
                note['accepted_evidence_fingerprint'] = suggestion.get('evidence_fingerprint')
                note['suggestion'] = None
                note['updated_at'] = now
                save_notes_doc(self.record, doc)
                return copy.deepcopy(note)

            if action == 'reject':
                if expected_revision is not None and int(expected_revision) != current_rev:
                    raise ValueError(f'版本冲突：当前版本为 {current_rev}，提供版本为 {expected_revision}')

                suggestion = note.get('suggestion')
                if suggestion_id:
                    if not suggestion or str(suggestion.get('id')) != str(suggestion_id):
                        raise ValueError('建议编号已过期或不存在')

                rejected_fp = None
                if suggestion:
                    rejected_fp = suggestion.get('evidence_fingerprint')
                    if not rejected_fp:
                        hasher = hashlib.sha256()
                        for ev in (suggestion.get('evidence') or []):
                            hasher.update(str(ev.get('citation_id', '')).encode('utf-8'))
                            hasher.update(str(ev.get('content_hash') or ev.get('revision') or ev.get('text', '')).encode('utf-8'))
                        rejected_fp = hasher.hexdigest()

                note['rejected_fingerprint'] = rejected_fp
                note['suggestion'] = None
                note['status'] = 'idle'
                note['revision'] = current_rev + 1
                note['updated_at'] = now
                save_notes_doc(self.record, doc)
                return copy.deepcopy(note)

            if action == 'undo':
                if expected_revision is not None and int(expected_revision) != current_rev:
                    raise ValueError(f'版本冲突：当前版本为 {current_rev}，提供版本为 {expected_revision}')
                if suggestion_id and note.get('suggestion') and str(note['suggestion'].get('id')) != str(suggestion_id):
                    raise ValueError('建议编号已过期')

                history = note.get('history') or []
                if not history:
                    raise ValueError('没有可撤销的历史版本')
                # histories undo does not destroy raw!
                snapshot = history.pop()
                note['history'] = history
                note['content'] = snapshot['content']
                for key in ('evidence','accepted_claims','manual_edit','anchor','start','end','last_model','accepted_evidence_fingerprint'):
                    if key in snapshot:
                        note[key]=copy.deepcopy(snapshot[key])
                note['text'] = note['content']
                note['revision'] = current_rev + 1
                if note.get('suggestion'):
                    note['suggestion']['stale'] = True
                note['updated_at'] = now
                save_notes_doc(self.record, doc)
                return copy.deepcopy(note)

            if action == 'cancel':
                note['auto_enhance_paused'] = True
                note['status'] = 'idle'
                note['job'] = {
                    'status': 'cancelled',
                    'retryable': False,
                    'cancelled_at': now,
                }
                note['updated_at'] = now
                save_notes_doc(self.record, doc)
                return copy.deepcopy(note)

            raise ValueError(f'不支持的操作: {action}')
