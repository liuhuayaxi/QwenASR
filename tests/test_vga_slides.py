import json
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

import av
import numpy as np
from PIL import Image, ImageDraw

from qwen_asr_app.native_pdf import pdf_operation
from qwen_asr_app.vga_slides import ExtractionCancelled, extract_slides, write_slides_pdf


def synthetic_video(path, states=None):
    first = Image.new('RGB', (160, 120), 'white')
    ImageDraw.Draw(first).rectangle((0, 0, 79, 119), fill='black')
    builds = []
    for count in range(1, 4):
        image = Image.new('RGB', (160, 120), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 8, 145, 18), fill='black')
        for bullet in range(count):
            draw.rectangle((15, 32 + bullet * 24, 100, 40 + bullet * 24), fill='black')
        builds.append(image)
    last = Image.new('RGB', (160, 120), 'white')
    ImageDraw.Draw(last).rectangle((0, 0, 159, 70), fill='black')
    transient = Image.new('RGB', (160, 120), 'gray')
    ImageDraw.Draw(transient).rectangle((50, 0, 150, 119), fill='black')
    if states is None:
        states = [(first, 2), (builds[0], 2), (transient, .5),
                  (builds[1], 2), (builds[2], 2),
                  (Image.new('RGB', first.size, 'black'), 1), (last, 2), (first, 2)]
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('mpeg4', rate=10)
        stream.width, stream.height = first.size
        stream.pix_fmt = 'yuv420p'
        for image, seconds in states:
            for _ in range(round(seconds * 10)):
                for packet in stream.encode(av.VideoFrame.from_image(image)):
                    output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return first, builds[-1], last


class VGASlideTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_final_builds_transients_black_gap_and_revisit(self):
        video = self.root / 'lecture.mp4'
        expected = synthetic_video(video)
        progress = []
        slides = extract_slides(video, progress=lambda elapsed, total: progress.append((elapsed, total)))
        self.assertEqual(len(slides), 3)
        for slide, target in zip(slides, expected):
            with Image.open(slide.image_path) as image:
                difference = np.abs(np.asarray(image, dtype=float) - np.asarray(target, dtype=float))
                self.assertLess(difference.mean(), 3)
            self.assertLessEqual(slide.start_sec, slide.captured_sec)
            self.assertEqual(slide.end_sec, slide.captured_sec)
        self.assertAlmostEqual(slides[1].start_sec, 2)
        self.assertAlmostEqual(slides[1].captured_sec, 8)
        self.assertEqual([s.index for s in slides], [1, 2, 3])
        self.assertTrue(all(a[0] < b[0] for a, b in zip(progress, progress[1:])))
        self.assertGreater(progress[-1][1], progress[-1][0])
        pdf = self.root / 'slides.pdf'
        manifest = write_slides_pdf(slides, pdf)
        self.assertEqual(pdf_operation('page_count', path=str(pdf)), 3)
        self.assertEqual(len(json.loads(json.dumps(manifest))['pages']), 3)
        self.assertEqual(len(extract_slides(video, skip_revisited=False)), 4)

    def test_cancel(self):
        video = self.root / 'cancel.mp4'
        synthetic_video(video)
        samples = []
        with self.assertRaises(ExtractionCancelled):
            extract_slides(video, progress=lambda *args: samples.append(args),
                           cancel=lambda: len(samples) >= 3)
        self.assertEqual(len(samples), 3)

    def test_resolution_switch_starts_new_slide(self):
        sizes = [(160, 120), (192, 108), (128, 96)]
        video = self.root / 'resolution-switch.ts'
        segments = []
        for index, size in enumerate(sizes):
            segment = self.root / f'segment-{index}.ts'
            image = Image.new('RGB', size, 'white')
            ImageDraw.Draw(image).rectangle((10, 10, 65, 70), fill='black')
            with av.open(str(segment), 'w') as output:
                stream = output.add_stream('mpeg2video', rate=10)
                stream.width, stream.height = size
                stream.pix_fmt = 'yuv420p'
                for number in range(20):
                    frame = av.VideoFrame.from_image(image)
                    frame.pts = index * 20 + number
                    frame.time_base = Fraction(1, 10)
                    for packet in stream.encode(frame):
                        output.mux(packet)
                for packet in stream.encode():
                    output.mux(packet)
            segments.append(segment.read_bytes())
        video.write_bytes(b''.join(segments))
        with av.open(str(video)) as container:
            decoded_sizes = {(frame.width, frame.height) for frame in container.decode(video=0)}
        self.assertEqual(decoded_sizes, set(sizes))
        slides = extract_slides(video, animation_threshold=1, duplicate_threshold=1)
        self.assertEqual(len(slides), 3)
        for slide, size in zip(slides, sizes):
            with Image.open(slide.image_path) as image:
                self.assertEqual(image.size, size)
            self.assertLess(slide.start_sec, slide.captured_sec)
        self.assertTrue(all(a.captured_sec < b.start_sec for a, b in zip(slides, slides[1:])))

    def test_blank_short_empty_and_errors(self):
        video = self.root / 'blank.mp4'
        synthetic_video(video, [(Image.new('RGB', (160, 120), 'white'), 2)])
        self.assertEqual(extract_slides(video), [])
        synthetic_video(video, [(Image.effect_noise((160, 120), 80).convert('RGB'), .5)])
        self.assertEqual(extract_slides(video), [])
        with self.assertRaises(ValueError):
            write_slides_pdf([], self.root / 'empty.pdf')
        with self.assertRaises(ValueError):
            extract_slides(video, sample_interval=0)
        with self.assertRaises(ValueError):
            extract_slides(video, animation_threshold=2)
        with self.assertRaises((OSError, av.error.FFmpegError)):
            extract_slides(self.root / 'missing.mp4')

    def test_duplicate_consecutive_stable_segments(self):
        video = self.root / 'duplicate.mp4'
        image = Image.new('RGB', (160, 120), 'white')
        ImageDraw.Draw(image).rectangle((0, 0, 70, 100), fill='black')
        synthetic_video(video, [(image, 2), (Image.new('RGB', image.size, 'black'), 1), (image, 2)])
        self.assertEqual(len(extract_slides(video, skip_revisited=False)), 1)


if __name__ == '__main__':
    unittest.main()
