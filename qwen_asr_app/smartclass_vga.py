"""Replay screen tracks imported as lesson-scoped course material."""
from fractions import Fraction
from pathlib import Path

from .parallel_media import parallel_media
from .smartclass import SmartClassError, media_url


def download_video(sources, destination, stop):
    import av

    offset = Fraction(0)
    output_stream = None
    with av.open(str(destination), 'w') as output:
        for item in sources:
            with parallel_media(media_url(item['url']), stop) as media, av.open(media) as source:
                stream = next(iter(source.streams.video), None)
                if stream is None:
                    raise SmartClassError('VGA 没有视频轨道')
                if output_stream is None:
                    output_stream = output.add_stream_from_template(stream)
                elif stream.codec_context.name != output_stream.codec_context.name:
                    raise SmartClassError('VGA 分段视频编码不一致')
                origin = None
                end = offset
                for packet in source.demux(stream):
                    if stop.is_set():
                        raise SmartClassError('VGA 下载已暂停')
                    if packet.dts is None or packet.pts is None:
                        continue
                    base = packet.time_base
                    if origin is None:
                        origin = packet.dts
                    packet.pts += round(offset / base) - origin
                    packet.dts += round(offset / base) - origin
                    end = max(end, (packet.pts + (packet.duration or 1)) * base)
                    packet.stream = output_stream
                    output.mux(packet)
                offset = end


async def extract_courseware(sources, record, category, stop):
    import asyncio
    import tempfile
    from .course_files import CourseFiles
    from .storage import app_settings
    from .smartclass import private_json
    from .vga_slides import extract_slides
    from .vga_filter import filter_slides, write_filtered_pdf
    from .vga_emphasis import apply_emphasis

    with tempfile.TemporaryDirectory(prefix='.smartclass-vga-', dir=record.directory) as raw:
        folder = Path(raw)
        video, pdf = folder / 'VGA.mp4', folder / 'slides.pdf'
        transcript = folder / 'transcript.json'
        private_json(transcript, {'segments': record.segments})

        def extract():
            download_video(sources, video, stop)
            slides = extract_slides(video, deduplicate=False, cancel=stop.is_set)
            return filter_slides(slides, cancel=stop.is_set)

        worker = asyncio.create_task(asyncio.to_thread(extract))
        try:
            result = await asyncio.shield(worker)
            await apply_emphasis(result, video, transcript=transcript, cancel=stop.is_set)
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
