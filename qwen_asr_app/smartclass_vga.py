"""Replay screen tracks imported as lesson-scoped course material."""
from dataclasses import replace
import json
from pathlib import Path

from .parallel_media import parallel_media
from .smartclass import SmartClassError, media_url


def extract_replay_slides(sources, stop, *, audio_durations=None):
    from .vga_slides import extract_slides

    slides = []
    offset = 0.0
    audio_durations = audio_durations or {}
    for item in sources:
        if stop.is_set():
            raise SmartClassError('VGA 提取已暂停')
        # Decode the original container independently. Re-muxing unrelated
        # MP4 packet timelines can reject valid school recordings.
        with parallel_media(media_url(item['url']), stop) as media:
            captured = extract_slides(media, deduplicate=False, cancel=stop.is_set)
        for slide in captured:
            slides.append(replace(slide, index=len(slides) + 1, start_sec=offset + slide.start_sec,
                                  end_sec=offset + slide.end_sec, captured_sec=offset + slide.captured_sec))
        offset += audio_durations.get(item.get('segment_id'), float(item['duration']))
    return slides


async def extract_courseware(sources, record, category, stop):
    import asyncio
    import tempfile
    from .course_files import CourseFiles
    from .storage import app_settings
    from .smartclass import private_json
    from .vga_filter import filter_slides, write_filtered_pdf
    from .vga_emphasis import apply_emphasis

    try:
        manifest = json.loads((record.directory / '课程来源.json').read_text())
    except (OSError, ValueError):
        manifest = {}
    # ASR joins the decoded audio, which can be shorter than the catalog duration.
    audio_durations = {source['segment_id']: source['duration_seconds']
                       for source in manifest.get('completed_sources', [])
                       if 'segment_id' in source and isinstance(source.get('duration_seconds'), (int, float))
                       and source['duration_seconds'] > 0}
    with tempfile.TemporaryDirectory(prefix='.smartclass-vga-', dir=record.directory) as raw:
        folder = Path(raw)
        pdf = folder / 'slides.pdf'
        transcript = folder / 'transcript.json'
        private_json(transcript, {'segments': record.segments})

        def extract():
            slides = extract_replay_slides(sources, stop, audio_durations=audio_durations)
            return filter_slides(slides, cancel=stop.is_set)

        worker = asyncio.create_task(asyncio.to_thread(extract))
        try:
            result = await asyncio.shield(worker)
            await apply_emphasis(result, None, transcript=transcript, cancel=stop.is_set)
            writer = asyncio.create_task(asyncio.to_thread(write_filtered_pdf, result, pdf, cancel=stop.is_set))
            worker = writer
            await asyncio.shield(writer)
            if stop.is_set():
                raise SmartClassError('VGA 提取已暂停')
            item, _ = CourseFiles(app_settings).import_file(category, pdf,
                record.metadata.get('title', '课堂') + '(VGA课件).pdf', '课件')
            return item['id']
        except asyncio.CancelledError:
            stop.set()
            await asyncio.gather(worker, return_exceptions=True)
            raise
