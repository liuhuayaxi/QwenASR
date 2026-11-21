"""Sample VGA recordings into final-state slide images, spooled to disk."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import tempfile
import weakref

import av
import numpy as np
from PIL import Image, ImageFilter


class ExtractionCancelled(Exception):
    pass


class _ImageStore:
    def __init__(self):
        self.name = tempfile.mkdtemp(prefix='qwen-vga-')
        self.cleanup = weakref.finalize(self, shutil.rmtree, self.name, ignore_errors=True)


@dataclass
class Slide:
    index: int
    start_sec: float
    end_sec: float
    captured_sec: float
    image_path: Path
    _owner: object = field(default=None, repr=False, compare=False)
    occurrences: list[dict] = field(default_factory=list)

    def manifest(self):
        return {key: getattr(self, key) for key in
                ('index', 'start_sec', 'end_sec', 'captured_sec')}


def extract_slides(video_path, *, sample_interval=0.5, min_stable_sec=1.0,
                   comparison_width=320, pixel_threshold=20,
                   stable_threshold=0.01, animation_threshold=0.3,
                   retention_threshold=0.1, duplicate_threshold=0.01,
                   blank_std=3.0, black_threshold=12,
                   skip_revisited=True, deduplicate=True, progress=None, cancel=None) -> list[Slide]:
    """Return disk-backed slides. Temporary images live as long as the Slides.

    progress(elapsed_seconds, total_seconds_or_None) runs for each sample;
    cancel() returning true raises ExtractionCancelled. Decode is seek-free.
    Comparisons are heuristic: small local replacements can resemble builds.
    """
    if sample_interval <= 0 or min_stable_sec <= 0 or comparison_width < 1:
        raise ValueError('Sampling interval, stability duration and width must be positive')
    for value in (stable_threshold, animation_threshold, retention_threshold, duplicate_threshold):
        if not 0 <= value <= 1:
            raise ValueError('Fraction thresholds must be between zero and one')
    if not 0 <= pixel_threshold <= 255 or blank_std < 0 or not 0 <= black_threshold <= 255:
        raise ValueError('Invalid pixel or blank threshold')
    owner = _ImageStore()
    folder = Path(owner.name)
    candidate_path = folder / 'candidate.png'
    slides, emitted = [], []
    previous = segment_last = candidate = None
    segment_image = None
    segment_start = segment_end = 0.0
    candidate_start = candidate_end = 0.0
    next_sample = 0.0
    last_time = -1.0

    def changed(a, b):
        if a.shape != b.shape:
            return 1.0
        return float(np.mean(np.abs(a - b) > pixel_threshold))

    def emit():
        nonlocal candidate
        if candidate is None:
            return
        references = emitted if skip_revisited else emitted[-1:]
        def duplicate(path):
            old = np.load(path).astype(np.int16)
            return candidate.shape == old.shape and changed(candidate, old) <= duplicate_threshold

        match = next((int(old.stem) - 1 for old in references if duplicate(old)), None) if deduplicate else None
        if match is not None:
            retained = slides[match]
            if not retained.occurrences:
                retained.occurrences.append(retained.manifest())
            retained.occurrences.append({'index': retained.index, 'start_sec': candidate_start,
                                         'end_sec': candidate_end, 'captured_sec': candidate_end})
        else:
            path = folder / f'{len(slides) + 1:04d}.png'
            shutil.copyfile(candidate_path, path)
            slides.append(Slide(len(slides) + 1, candidate_start, candidate_end,
                                candidate_end, path, owner))
            comparison = folder / f'{len(slides):04d}.npy'
            np.save(comparison, candidate.astype(np.uint8))
            emitted.append(comparison)
        candidate = None

    def finish_segment():
        nonlocal candidate, candidate_start, candidate_end
        if segment_last is None or segment_end - segment_start + 1e-6 < min_stable_sec:
            return
        merge = False
        if candidate is not None and candidate.shape == segment_last.shape:
            background = float(np.median(candidate))
            ink = np.abs(candidate - background) > pixel_threshold
            erased = ink & (np.abs(segment_last - background) <= pixel_threshold)
            loss = float(erased.sum() / max(1, ink.sum()))
            merge = (changed(candidate, segment_last) <= animation_threshold
                     and loss <= retention_threshold)
        if not merge:
            emit()
            candidate_start = segment_start
        candidate = segment_last.copy()
        candidate_end = segment_end
        segment_image.save(candidate_path)

    try:
        with av.open(str(video_path)) as container:
            if not container.streams.video:
                raise ValueError('File contains no video stream')
            stream = container.streams.video[0]
            stream.thread_type = 'AUTO'
            total = float(stream.duration * stream.time_base) if stream.duration is not None else None
            origin = float(stream.start_time * stream.time_base) if stream.start_time is not None else None
            for frame in container.decode(stream):
                if cancel and cancel():
                    raise ExtractionCancelled('Slide extraction cancelled')
                if frame.pts is None:
                    continue
                timestamp = float(frame.pts * frame.time_base)
                if origin is None:
                    origin = timestamp
                timestamp = max(0.0, timestamp - origin)
                if timestamp < next_sample or timestamp <= last_time:
                    continue
                next_sample = timestamp + sample_interval - 1e-6
                last_time = timestamp
                if progress:
                    progress(timestamp, total)
                image = frame.to_image().convert('RGB')
                width = min(comparison_width, image.width)
                small = image.resize((width, max(1, round(image.height * width / image.width))))
                gray = np.asarray(small.convert('L').filter(ImageFilter.GaussianBlur(0.6)), dtype=np.int16)
                blank = float(gray.std()) < blank_std or float(np.mean(gray < black_threshold)) > 0.98
                if blank:
                    finish_segment()
                    emit()
                    previous = segment_last = None
                    segment_image = None
                    continue
                if previous is None or changed(previous, gray) >= stable_threshold:
                    finish_segment()
                    segment_start = timestamp
                segment_end = timestamp
                segment_last = gray
                previous = gray
                segment_image = image
            finish_segment()
            emit()
        if not slides:
            owner.cleanup()
        return slides
    except BaseException:
        owner.cleanup()
        raise


def write_slides_pdf(slides, out_pdf):
    """Write image-only pages incrementally; return a JSON-able page manifest."""
    if not slides:
        raise ValueError('No slides to write')
    destination = Path(out_pdf)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The native single-image helper cannot append pages without another PDF runtime.
    with tempfile.TemporaryDirectory(prefix='.vga-pdf-', dir=destination.parent) as raw:
        staged = Path(raw) / 'slides.pdf'
        for index, slide in enumerate(slides):
            with Image.open(slide.image_path) as image:
                image.convert('RGB').save(staged, 'PDF', append=index > 0, resolution=72)
        staged.replace(destination)
    return {'pages': [slide.manifest() for slide in slides]}
