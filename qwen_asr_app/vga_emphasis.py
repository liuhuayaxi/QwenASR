"""Transcript-only annotations for retained VGA pages."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re
import uuid

from .paths import PRIVATE_ROOT
from .remote_operations import discard_completed_payloads
from .vga_slides import ExtractionCancelled


EMPHASIS_KEYWORDS = ('重点', '考点', '考试', '必考', '会考', '要考', '期末')
EMPHASIS_PATTERN = re.compile('|'.join(EMPHASIS_KEYWORDS) + r'|(?:这个|这里|这点|这一点)(?:非常|很|特别)?重要')
EMPHASIS_MIN_MINUTES = 1.0


def normalize_segments(segments):
    result = []
    if not isinstance(segments, list):
        raise ValueError('转写文件必须包含分段列表')
    for segment in segments:
        start, end = float(segment['start']), float(segment['end'])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError('转写分段时间无效')
        result.append({'start': start, 'end': end,
                       'raw': str(segment.get('raw') or segment.get('text') or '')})
    return sorted(result, key=lambda segment: (segment['start'], segment['end']))


def load_transcript(path):
    path = Path(path).expanduser()
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() == '.json':
        value = json.loads(text)
        return normalize_segments(value.get('segments') if isinstance(value, dict) else value)

    def seconds(value):
        total = 0.0
        for part in value.split(':'):
            total = total * 60 + float(part)
        return total

    matches = list(re.finditer(r'^\[([\d:.]+)\s*-\s*([\d:.]+)\]\s*', text, re.M))
    if not matches and text.strip():
        raise ValueError('无法识别转写文本，请使用带 [开始 - 结束] 时间的格式')
    return normalize_segments([
        {'start': seconds(match[1]), 'end': seconds(match[2]),
         'raw': text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()}
        for i, match in enumerate(matches)])


def mark_emphasis(result, segments):
    segments = normalize_segments(segments)
    windows = [(i, w['start_sec'], w['end_sec'])
               for i, page in enumerate(result.pages) for w in page.occurrences]
    page_hits = [[] for page in result.pages]
    for segment in segments:
        raw = segment['raw']
        for match in EMPHASIS_PATTERN.finditer(raw):
            if not windows:
                continue
            fraction = (match.start() + match.end()) / (2 * len(raw))
            hit_sec = segment['start'] + (segment['end'] - segment['start']) * fraction
            index, _, _ = min(windows, key=lambda w: (
                max(w[1] - hit_sec, 0, hit_sec - w[2]),
                not (w[1] <= hit_sec < w[2]), w[0]))
            page_hits[index].append({
                'start': segment['start'], 'end': segment['end'], 'hit_sec': hit_sec,
                'text': raw[max(0, match.start() - 40):match.end() + 40]})
    candidates = []
    for page, hits in zip(result.pages, page_hits):
        overlapping = [s for s in segments if any(
            s['start'] < window['end_sec'] and s['end'] > window['start_sec']
            for window in page.occurrences)]
        page.transcript_text = '\n'.join(s['raw'] for s in overlapping)
        count = len(hits)
        minutes = sum(max(0, w['end_sec'] - w['start_sec']) for w in page.occurrences) / 60
        page.emphasis_score = count / max(EMPHASIS_MIN_MINUTES, minutes)
        page.important = False
        if count:
            candidates.append(page)
        page.matched_snippets = hits[:3]
    limit = max(1, math.floor(len(result.pages) * .2 + .5))
    for page in sorted(candidates, key=lambda page: -page.emphasis_score)[:limit]:
        page.important = True
    return result


def find_audio(video, audio=None):
    if audio:
        path = Path(audio).expanduser().resolve()
        if not path.is_file():
            raise ValueError('录音文件不存在，请检查本机录音路径')
        return path
    video = Path(video).expanduser().resolve()
    for extension in ('.aac', '.m4a', '.mp3', '.wav'):
        sibling = video.with_suffix(extension)
        if sibling.is_file():
            return sibling
    import av
    with av.open(str(video)) as container:
        if container.streams.audio:
            return video
    raise ValueError('未找到同名录音，视频也没有音轨')


async def transcribe_audio(path, category, progress=None):
    folder = PRIVATE_ROOT / 'uploads' / ('vga-' + uuid.uuid4().hex) / 'remote'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        return await _transcribe_audio(path, category, progress, folder.parent)
    finally:
        await asyncio.to_thread(discard_completed_payloads, folder / 'receipt.json')
        try:
            folder.rmdir()
            folder.parent.rmdir()
        except OSError:
            pass


async def _transcribe_audio(path, category, progress, folder):
    from app import batch_transcription_stream
    from .inference_providers import inference_settings

    ident = uuid.uuid4().hex
    config = {'category': category, 'title': Path(path).stem + '（VGA录音）',
              'ai_correction': False, 'translation': 'off',
              '_remote_operation_dir': str(folder / 'remote'),
              '_remote_operation_key': ident, '_upload_recording_id': 'upload-' + ident,
              'inference_settings': inference_settings.for_new_task('asr')}
    completed = None
    stream = batch_transcription_stream(Path(path), Path(path).name, config, preserve_source=True)
    try:
        async for payload in stream:
            event = json.loads(payload)
            if progress:
                progress(event.get('stage') or '正在转写录音')
            if event['type'] == 'error':
                raise RuntimeError(event.get('message') or '录音转写失败')
            if event['type'] == 'complete':
                completed = event['session']
    finally:
        await stream.aclose()
    if completed is None:
        raise RuntimeError('录音转写未完成')
    return normalize_segments(completed['segments']), completed['id']


async def apply_emphasis(result, video, *, audio=None, transcript=None, enabled=True,
                         category='未分类', cancel=None, progress=None, close_runtime=False):
    if not enabled:
        return result

    async def work():
        if transcript:
            segments = await asyncio.to_thread(load_transcript, transcript)
        else:
            path = await asyncio.to_thread(find_audio, video, audio)
            try:
                segments, recording_id = await transcribe_audio(path, category, progress)
            finally:
                if close_runtime:
                    import sys
                    runtime = sys.modules.get('app')
                    if runtime is not None:
                        await runtime.model_manager.close()
            result.recording_id = recording_id
        mark_emphasis(result, segments)

    task = asyncio.create_task(work())
    try:
        while not task.done():
            if cancel and cancel():
                raise ExtractionCancelled('已取消录音重点标记')
            await asyncio.wait({task}, timeout=.2)
        await task
    except (asyncio.CancelledError, ExtractionCancelled):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception as exc:
        result.warnings.append(f'录音重点标记失败，已保留全部课件页面：{type(exc).__name__}：{exc}')
    return result
