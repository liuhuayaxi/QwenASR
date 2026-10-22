"""Small list DTOs, server-side filters and conditional responses."""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi.responses import Response


def conditional(payload, request=None):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
    headers = {'ETag': etag, 'Cache-Control': 'private, no-cache'}
    if request is not None and request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    return Response(raw, media_type='application/json', headers=headers)


def day_key(record):
    metadata = record if isinstance(record, dict) else record.metadata
    value = str(metadata.get('started_at') or '')
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    except ValueError:
        return ''


def listing(records, category='', date='', offset=0, limit=0):
    counts = {};calendar={};selected=[]
    for record in records:
        metadata = record if isinstance(record, dict) else record.metadata
        has_note = bool(metadata.get('has_note')) if isinstance(record, dict) else bool((metadata.get('ai_note_summary') or {}).get('summary'))
        course = metadata.get('category') or '未分类'
        item = counts.setdefault(course, {'records': 0, 'notes': 0})
        item['records'] += 1
        item['notes'] += has_note
        if category and metadata.get('category')!=category:continue
        day = day_key(record)
        if day:
            item = calendar.setdefault(day, {'count': 0, 'duration': 0})
            item['count'] += 1
            item['duration'] += round(metadata.get('duration', 0) if isinstance(record, dict) else record.duration, 1)
        if not date or day==date:
            selected.append((str(metadata.get('started_at') or ''), record if isinstance(record, dict) else record.list_item(), has_note))
    selected.sort(key=lambda row:row[0],reverse=True)
    total = len(selected)
    page = selected[offset:offset + limit] if limit else selected[offset:]
    return {'records': [row[1] for row in page], 'total': total,
            'offset': offset, 'limit': limit, 'next_offset': offset + len(page) if offset + len(page) < total else None,
            'course_counts': counts, 'calendar': calendar,
            'note_count': sum(row[2] for row in selected)}
