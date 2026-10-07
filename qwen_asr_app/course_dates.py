"""Separate SmartClass lesson timestamps from local import/archive timestamps."""
from datetime import datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


def platform_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace('/', '-'))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SHANGHAI)
    return stamp.astimezone(SHANGHAI).isoformat(timespec='seconds')


def apply_course_dates(metadata, directory):
    try:
        manifest = json.loads((Path(directory) / '课程来源.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    if not (metadata.get('course_stream') or metadata.get('course_time') or manifest
            or str(metadata.get('source_filename', '')).startswith('smartclass_')):
        return False
    times = metadata.get('course_time') or {}
    sources = [*((metadata.get('course_stream') or {}).get('sources') or []), *(manifest.get('sources') or [])]
    starts = [platform_time(item.get('start') or item.get('source_start')) for item in sources if isinstance(item, dict)]
    ends = [platform_time(item.get('end') or item.get('source_end')) for item in sources if isinstance(item, dict)]
    # Older manifests retain the platform's full range in their generated title.
    match = re.search(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s*[–—-]\s*(\d{2}:\d{2}(?::\d{2})?)',
                      str(manifest.get('title') or ''))
    if match:
        starts.append(platform_time(f'{match[1]} {match[2]}'))
        ends.append(platform_time(f'{match[1]} {match[3]}'))
    starts.append(platform_time(times.get('start')))
    ends.append(platform_time(times.get('end')))
    start = min(filter(None, starts), default=None)
    if not start and metadata.get('recorded_at') != metadata.get('started_at'):
        start = platform_time(metadata.get('recorded_at'))
    end = max(filter(None, ends), default=None)
    if not start:
        return False
    updates = {'course_time': {'start': start, 'end': end}, 'recorded_at': start, 'started_at': start}
    if metadata.get('started_at') and not metadata.get('imported_at'):
        updates['imported_at'] = metadata['started_at']
    if end and end >= start and metadata.get('status') == 'completed':
        if metadata.get('ended_at') and metadata['ended_at'] != end and not metadata.get('archived_at'):
            updates['archived_at'] = metadata['ended_at']
        updates['ended_at'] = end
    changed = any(metadata.get(key) != value for key, value in updates.items())
    metadata.update(updates)
    return changed
