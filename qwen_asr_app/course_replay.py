"""Map merged recording clocks back to the original SmartClass media segments."""
import json
import math
import re
from urllib.parse import urlencode


def _seconds(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else 0
    except (TypeError, ValueError):
        return 0


def _decoded_durations(chunks):
    """Trust only continuous cursors from a source's start through its boundary."""
    durations = {}
    source, samples, covered = 0, 0, True
    for chunk in chunks:
        cursor = chunk.get('cursor') if isinstance(chunk, dict) else None
        size = chunk.get('samples') if isinstance(chunk, dict) else None
        index = cursor.get('source') if isinstance(cursor, dict) else None
        end = cursor.get('samples') if isinstance(cursor, dict) else None
        if any(type(value) is not int for value in (size, index, end)) or size <= 0 or index < 0 or end < 0:
            covered = False
            continue
        if index < source:
            return {}  # Reordered or duplicate chunks cannot prove coverage.
        if end == 0:
            if index == source + 1 and covered:
                durations[source] = (samples + size) / 16000
            source, samples, covered = index, 0, True
        else:
            covered = covered and index == source and end == samples + size
            source, samples = index, end
    return durations


def replay_sources(record):
    stream = record.metadata.get('course_stream') or {}
    try:
        manifest = json.loads((record.directory / '课程来源.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    sources = stream.get('sources') or manifest.get('sources') or []
    if not isinstance(sources, list):
        return []
    completed = {(str(item.get('video_id')), str(item.get('segment_id'))): item
                 for item in manifest.get('completed_sources', []) if isinstance(item, dict)}
    legacy = {(str(item.get('video_id')), str(item.get('segment_id'))): item
              for item in manifest.get('sources', []) if isinstance(item, dict)}
    downloaded = _decoded_durations(stream.get('chunks') or [])
    result, offset = [], 0.0
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            return []
        video_id, segment_id = str(item.get('video_id') or ''), str(item.get('segment_id') or '')
        if not re.fullmatch(r'[0-9a-fA-F]{32}', video_id) or not re.fullmatch(r'[1-9][0-9]*', segment_id):
            return []
        done = completed.get((video_id, segment_id), {})
        old = legacy.get((video_id, segment_id), {})
        duration = (_seconds(done.get('duration_seconds')) or _seconds(item.get('duration_seconds'))
                    or _seconds(old.get('duration_seconds')) or downloaded.get(index, 0)
                    or _seconds(item.get('duration')))
        if not duration:
            return []
        result.append({'video_id': video_id, 'segment_id': segment_id,
                       'offset_seconds': round(offset, 6), 'duration_seconds': duration,
                       'source_offset_seconds': _seconds(item.get('source_offset_seconds', old.get('source_offset_seconds'))),
                       'title': str(item.get('title') or item.get('source_title') or ''),
                       'start': str(item.get('start') or item.get('source_start') or '')})
        offset += duration
    return result


def replay_target(record, seconds):
    if not math.isfinite(seconds) or seconds < 0 or seconds > 7200:
        raise ValueError('回放时间无效')
    sources = replay_sources(record)
    if not sources:
        raise ValueError('这条录音没有可用的 Smart Class 来源记录')
    total = sources[-1]['offset_seconds'] + sources[-1]['duration_seconds']
    if seconds > total + 1:
        raise ValueError('回放时间超过这堂课的长度')
    selected = next((item for item in sources
                     if seconds < item['offset_seconds'] + item['duration_seconds']), sources[-1])
    relative = max(0, min(seconds - selected['offset_seconds'], selected['duration_seconds'] - .05))
    relative += selected['source_offset_seconds']
    params = urlencode({'NewID': selected['video_id'], 'SegementID': selected['segment_id']})
    return {'recording_id': record.id, 'recording_title': record.metadata.get('title', ''),
            'video_id': selected['video_id'], 'segment_id': selected['segment_id'],
            'seconds': round(relative, 3), 'recording_seconds': seconds,
            'source_title': selected['title'],
            'url': 'https://njupt.smartclass.cn/PlayPages/Video.aspx?' + params}
