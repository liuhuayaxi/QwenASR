"""Source identity and append-only classroom supplements."""
import hashlib
import json
import time
from pathlib import Path

from .lazy import lazy_module
from fastapi import HTTPException

from .audio import decode_audio_file
from .durable import atomic_bytes, atomic_json
from .settings import SAMPLE_RATE
from .course_replay import replay_sources

np = lazy_module("numpy")


def source_keys(sources):
    return [str(s['video_id']) + ':' + str(s['segment_id']) for s in sources]


def record_sources(record):
    sources = record.metadata.get('course_stream', {}).get('sources')
    if sources:
        return sources
    try:
        return json.loads((record.directory / '课程来源.json').read_text()).get('sources', [])
    except (OSError, ValueError):
        return []


def match_record(records, key, sources):
    wanted = source_keys(sources)
    matches = []
    for record in records:
        old = source_keys(record_sources(record))
        exact = record.metadata.get('source_filename') == 'smartclass_' + key + '.flac'
        alias = key in record.metadata.get('course_stream', {}).get('keys', [])
        if exact or alias or (wanted and set(old).intersection(wanted)):
            matches.append((record, old))
    if len(matches) > 1:
        raise HTTPException(409, '选中回放涉及多份已有笔记，请先在资料库核对，已停止重复导入')
    if not matches:
        return None, False
    record, old = matches[0]
    if wanted and old:
        if wanted == old or wanted == old[:len(wanted)]:
            return record, False
        if old == wanted[:len(old)]:
            return record, True
        raise HTTPException(409, '回放顺序与已有笔记不一致，已保留原笔记，请核对课次')
    return record, False


def supplement(record, sources, key, duration, title):
    """Prepare cached prefix before committing the new classroom identity."""
    if record.metadata.get('status') != 'completed':
        raise HTTPException(409, '请先完成或继续第一节，再选择合并课次补齐')
    state = record.metadata.setdefault('course_stream', {})
    if not state.get('chunks'):
        count = int(record.metadata.get('sample_count', 0))
        state['chunks'] = [{'samples': min(120 * SAMPLE_RATE, count - start)}
                           for start in range(0, count, 120 * SAMPLE_RATE)]
    state.setdefault('keys', [str(record.metadata.get('source_filename', '')).removeprefix('smartclass_').removesuffix('.flac')])
    if any(record.metadata.get(k, {}).get('status') == 'running' for k in ('summary_job', 'ai_job')):
        raise HTTPException(409, '已有笔记任务正在处理，请完成后再补齐课次')
    previous_sources = record_sources(record)
    actual = {(s['video_id'], s['segment_id']): s for s in replay_sources(record)}
    sources = [dict(s, **({'duration_seconds': actual[(str(s['video_id']), str(s['segment_id']))]['duration_seconds']}
                         if (str(s['video_id']), str(s['segment_id'])) in actual else {})) for s in sources]
    # A completed lesson retains only its daily AAC. Rebuild a temporary
    # prefix from it when another period is appended.
    cache = record.audio_path
    lossless = False
    audio = decode_audio_file(cache if lossless else record.audio_path)
    count = int(record.metadata['sample_count'])
    if not lossless:
        # Our older ADTS encoder adds a 1024-sample priming frame.
        priming = min(1024, max(0, len(audio) - count))
        audio = audio[priming:]
    if len(audio) < count:
        raise HTTPException(409, '本地第一节音频长度不足，原录音已保留，请核对音频')
    audio = np.clip(np.rint(audio[:count] * 32768), -32768, 32767).astype('<i2')
    folder = record.directory / '.course-chunks'
    folder.mkdir(exist_ok=True)
    cursor = 0
    rebuilt = []
    for index, chunk in enumerate(state['chunks']):
        end = cursor + chunk['samples']
        raw = audio[cursor:end].tobytes()
        item = {**chunk, 'sha256': hashlib.sha256(raw).hexdigest()}
        atomic_bytes(folder / f'{index:05}.pcm', raw)
        atomic_json(folder / f'{index:05}.json', item)
        rebuilt.append(item)
        cursor = end
    if cursor != count or not rebuilt:
        raise HTTPException(409, '本地音频断点与第一节不一致，已保留原笔记')
    rebuilt[-1]['cursor'] = {'source': len(previous_sources), 'samples': 0}
    atomic_json(folder / f'{len(rebuilt)-1:05}.json', rebuilt[-1])
    old_title = state.get('automatic_title')
    if not old_title:
        try:
            old_title = json.loads((record.directory / '课程来源.json').read_text()).get('title')
        except (OSError, ValueError):
            pass
    if old_title and record.metadata.get('title') == old_title:
        record.metadata['title'] = title
    for field in ('download_wall_seconds', 'archive_seconds'):
        state.pop(field, None)
    state.update(chunks=rebuilt, sources=sources, expected_seconds=duration, input_complete=False,
                 completed_chunks=len(rebuilt), automatic_title=title, paused=False,
                 prefix_cache='lossless' if lossless else 'legacy_aac')
    state.setdefault('keys', []).append(key)
    state.setdefault('supplements', []).append({'at': time.time(), 'prefix_samples': count})
    for name in ('summary_job', 'ai_note_summary_draft'):
        record.metadata.pop(name, None)
    if record.metadata.get('ai_note_summary'):
        record.metadata['ai_note_summary']['stale'] = True
    record.metadata['status'] = 'interrupted'
    record.save_all()
