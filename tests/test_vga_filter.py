from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf
from PIL import Image, ImageDraw

from qwen_asr_app.vga_filter import classify_page, filter_slides, write_filtered_pdf
from qwen_asr_app.vga_slides import ExtractionCancelled, Slide


def fixture(kind='slide'):
    image = Image.new('RGB', (512, 384), 'white')
    draw = ImageDraw.Draw(image)
    if kind == 'junk':
        draw.rectangle((0, 0, 511, 383), fill='#2079c9')
        draw.rectangle((0, 362, 511, 383), fill='#303030')
        for y in range(20, 250, 60):
            for x in range(15, 120, 40):
                draw.rectangle((x, y, x + 20, y + 25), fill='yellow')
        return image
    if kind == 'document':
        draw.rectangle((0, 0, 511, 110), fill='#dddddd')
        for y in range(10, 100, 15):
            for x in range(10, 500, 12):
                draw.rectangle((x, y, x + 3, y + 3), fill='#555555')
        draw.rectangle((0, 111, 110, 360), fill='#eeeeee')
        for y in range(125, 340, 50):
            draw.rectangle((10, y, 85, y + 35), fill='white', outline='#aaaaaa')
        draw.rectangle((0, 362, 511, 383), fill='#dddddd')
    for y in range(135, 300, 20):
        for x in range(140, 390, 14):
            draw.rectangle((x, y, x + 5, y + 4), fill='black')
    return image


class VGAFilterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def slides(self, images):
        slides = []
        for index, image in enumerate(images, 1):
            path = self.root / f'{index}.png'
            image.save(path)
            slides.append(Slide(index, index * 3, index * 3 + 2, index * 3 + 2, path))
        return slides

    def test_visual_chrome_and_colored_footer(self):
        for kind in ('slide', 'document', 'junk'):
            image = fixture(kind)
            self.assertEqual(classify_page(image)[0], kind)
            if kind != 'junk':
                ImageDraw.Draw(image).rectangle((0, 350, 511, 383), fill='red')
                self.assertEqual(classify_page(image)[0], kind)
        viewer = fixture()
        draw = ImageDraw.Draw(viewer)
        draw.rectangle((0, 0, 511, 31), fill='#252525')
        draw.rectangle((0, 40, 89, 350), fill='white')
        draw.rectangle((94, 40, 108, 350), fill='#f3f3f3')
        for y in range(45, 340, 50):
            draw.rectangle((10, y, 80, y + 35), outline='#cccccc')
        self.assertEqual(classify_page(viewer)[0], 'junk')
        self.assertEqual(classify_page(Image.new('RGB', (512, 384), '#2079c9'))[0], 'slide')

    def test_smoothing_sections_and_overlay(self):
        slides = self.slides([fixture('document'), fixture(), fixture(), fixture('document'), fixture('junk')])
        with patch('qwen_asr_app.vga_filter.comparison_metrics', return_value=(1, 1, 1)):
            result = filter_slides(slides)
        self.assertEqual([p.kind for p in result.pages], ['document'] * 4)
        self.assertEqual(result.decisions[-1]['reason'], 'junk')
        slide = Slide(10, 20, 22, 22, slides[1].image_path)
        with patch('qwen_asr_app.vga_filter.comparison_metrics', return_value=(1, 1, 1)):
            result = filter_slides([*slides, slide])
        self.assertEqual(result.pages[0].kind, 'slide')
        output = self.root / 'sections.pdf'
        manifest = write_filtered_pdf(result, output)
        self.assertEqual(manifest['pages'][0]['section'], '课件')
        with pymupdf.open(output) as pdf:
            self.assertEqual([row[1] for row in pdf.get_toc()], ['课件', '作业讲解'])
            self.assertNotIn('作业讲解', pdf[0].get_text())
            self.assertIn('作业讲解', pdf[1].get_text())
            self.assertEqual(len(pdf[1].get_images()), 1)

    def test_near_duplicates_and_subset_keep_earlier_position(self):
        base = fixture()
        near = base.copy()
        ImageDraw.Draw(near).rectangle((480, 330, 482, 332), fill='black')
        complete = base.copy()
        ImageDraw.Draw(complete).rectangle((140, 305, 380, 320), fill='black')
        slides = self.slides([base, near, complete])
        result = filter_slides(slides)
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].position, 0)
        self.assertEqual(result.pages[0].slide.index, 3)
        self.assertEqual(result.pages[0].slide.start_sec, slides[2].start_sec)
        self.assertEqual(result.pages[0].occurrences, [slide.manifest() for slide in slides])
        self.assertEqual(result.pages[0].slide.captured_sec, slides[2].captured_sec)
        self.assertEqual([d['duplicate_of'] for d in result.decisions[:2]], [3, 3])
        self.assertEqual(filter_slides(self.slides([complete, base])).pages[0].slide.index, 1)

    def test_switches_empty_errors_and_cancel(self):
        slides = self.slides([fixture(), fixture('document'), fixture('junk')])
        result = filter_slides(slides)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual(len(filter_slides(slides, keep_homework=False).pages), 1)
        unfiltered = filter_slides(slides, enabled=False, keep_homework=False)
        self.assertEqual([p.slide.index for p in unfiltered.pages], [1, 2, 3])
        write_filtered_pdf(unfiltered, self.root / 'old.pdf')
        self.assertEqual(filter_slides([]).pages, [])
        with self.assertRaisesRegex(ValueError, '过滤后没有'):
            write_filtered_pdf(filter_slides([]), self.root / 'empty.pdf')
        with self.assertRaises(ExtractionCancelled):
            filter_slides(slides, cancel=lambda: True)
        with self.assertRaises(ExtractionCancelled):
            write_filtered_pdf(result, self.root / 'cancel.pdf', cancel=lambda: True)
        with self.assertRaises(FileNotFoundError):
            filter_slides([Slide(1, 0, 1, 1, self.root / 'missing.png')])
        self.assertFalse((self.root / 'cancel.pdf').exists())

    def test_no_cross_kind_or_shape_deduplication_and_long_runs(self):
        images = [fixture('document'), fixture(), fixture(), fixture(), fixture('document')]
        result = filter_slides(self.slides(images))
        self.assertEqual([d['kind'] for d in result.decisions], ['document', 'slide', 'slide', 'slide', 'document'])
        self.assertEqual([p.kind for p in result.pages], ['slide', 'document'])
        result = filter_slides(self.slides([fixture(), fixture().resize((512, 300))]))
        self.assertEqual(len(result.pages), 2)
