from tests import isolation  # noqa: F401

import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import pymupdf

from qwen_asr_app.vga_emphasis import (
    apply_emphasis, find_audio, load_transcript, mark_emphasis, transcribe_audio,
)
from qwen_asr_app.vga_filter import filter_slides, write_filtered_pdf
from qwen_asr_app.vga_slides import ExtractionCancelled, Slide, extract_slides
from tests.test_vga_filter import fixture
from tests.test_vga_slides import synthetic_video
from scripts.extract_vga_slides import main as cli_main


class VGAEmphasisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / 'page.png'
        fixture('document').save(self.image)

    def result(self):
        return filter_slides([Slide(i, start, start + 10, start + 9, self.image)
                              for i, start in enumerate((0, 100), 1)])

    def test_overlap_duplicate_windows_keywords_and_overlay(self):
        result = self.result()
        self.assertEqual(len(result.pages), 1)
        segments = [{'start': 10, 'end': 99, 'raw': '重点但不重叠'},
                    {'start': 1, 'end': 2, 'raw': '普通说明'},
                    *[{'start': 101 + i, 'end': 102 + i, 'raw': '考试重点，一定要记住'}
                      for i in range(4)]]
        mark_emphasis(result, segments)
        page = result.pages[0]
        self.assertTrue(page.important)
        self.assertEqual(len(page.matched_snippets), 3)
        self.assertEqual(page.matched_snippets[0]['start'], 101)
        self.assertIn('普通说明', page.transcript_text)
        self.assertNotIn('不重叠', page.transcript_text)
        self.assertEqual(page.transcript_text.count('考试重点'), 4)
        output = self.root / 'marked.pdf'
        manifest = write_filtered_pdf(result, output)
        self.assertTrue(manifest['pages'][0]['important'])
        with pymupdf.open(output) as pdf:
            self.assertIn('★', pdf.get_toc()[1][1])
            labels = {word: pdf[0].search_for(word)[0] for word in ('重点', '作业讲解')}
            self.assertFalse(labels['重点'].intersects(labels['作业讲解']))
            red = [span for block in pdf[0].get_text('dict')['blocks'] if 'lines' in block
                   for line in block['lines'] for span in line['spans'] if '重点' in span['text']]
            self.assertEqual(len(red), 1)
            self.assertGreater((red[0]['color'] >> 16) & 255, 150)
        mark_emphasis(result, [])
        self.assertFalse(page.important)
        self.assertEqual(page.transcript_text, '')
        self.assertEqual(len(result.pages), 1)

    def test_json_text_empty_and_invalid_transcripts(self):
        path = self.root / 'transcript.json'
        segments = [{'start': 62.5, 'end': 65, 'raw': '这是重点'}]
        path.write_text(json.dumps(segments))
        self.assertEqual(load_transcript(path), segments)
        path.write_text(json.dumps({'segments': segments}))
        self.assertEqual(load_transcript(path), segments)
        text = self.root / 'transcript.txt'
        text.write_text('[01:02.5 - 01:05] 这是重点\n', encoding='utf-8')
        self.assertEqual(load_transcript(text), segments)
        text.write_text('')
        self.assertEqual(load_transcript(text), [])
        for value in ('没有时间戳', '[20 - 10] 注意', '[1 - 2] 注意\n[4 - 3] 重点'):
            text.write_text(value)
            with self.assertRaises(ValueError):
                load_transcript(text)
        path.write_text('invalid')
        with self.assertRaises(ValueError):
            load_transcript(path)

    def test_sibling_explicit_and_video_audio_detection(self):
        video = self.root / 'lecture.mp4'
        synthetic_video(video)
        with self.assertRaisesRegex(ValueError, '没有音轨'):
            find_audio(video)
        for extension in ('.wav', '.mp3', '.m4a', '.aac'):
            sibling = video.with_suffix(extension)
            sibling.touch()
            self.assertEqual(find_audio(video), sibling)
        explicit = self.root / 'separate.wav'
        explicit.touch()
        self.assertEqual(find_audio(video, explicit), explicit)
        with self.assertRaisesRegex(ValueError, '录音文件不存在'):
            find_audio(video, self.root / 'missing.aac')
        for extension in ('.wav', '.mp3', '.m4a', '.aac'):
            video.with_suffix(extension).unlink()
        with patch('av.open') as opened:
            opened.return_value.__enter__.return_value.streams.audio = [object()]
            self.assertEqual(find_audio(video), video)

    def test_revisited_windows_survive_extraction_and_postprocessing(self):
        video = self.root / 'lecture.mp4'
        synthetic_video(video)
        slides = extract_slides(video, deduplicate=False)
        self.assertEqual(len(slides), 4)
        result = filter_slides(slides)
        self.assertEqual(len(result.pages), 3)
        first = result.pages[0]
        self.assertEqual(len(first.occurrences), 2)
        mark_emphasis(result, [{'start': slides[-1].start_sec + .1,
                               'end': slides[-1].end_sec, 'raw': '这是考试重点'}])
        self.assertTrue(first.important)

    async def test_failure_disabled_transcript_and_cancel(self):
        result = self.result()
        with patch('qwen_asr_app.vga_emphasis.find_audio', return_value=self.image), \
             patch('qwen_asr_app.vga_emphasis.transcribe_audio', new_callable=AsyncMock) as transcribe:
            transcribe.side_effect = ImportError('缺少 mlx_audio')
            await apply_emphasis(result, self.image)
            self.assertIn('缺少 mlx_audio', result.warnings[0])
            self.assertEqual(len(result.pages), 1)
            self.assertFalse(result.pages[0].important)
            transcribe.reset_mock()
            await apply_emphasis(result, self.image, enabled=False)
            transcribe.assert_not_called()
            transcript = self.root / 'transcript.json'
            transcript.write_text('[{"start": 101, "end": 102, "raw": "重点"}]')
            await apply_emphasis(result, self.image, transcript=transcript)
            transcribe.assert_not_called()
            self.assertTrue(result.pages[0].important)
            transcribe.side_effect = lambda *args: None
            with self.assertRaises(ExtractionCancelled):
                await apply_emphasis(result, self.image, cancel=lambda: True)

    async def test_existing_upload_pipeline_provider_and_archive(self):
        calls, closed = [], []
        async def stream(path, name, config, **options):
            calls.append((path, name, config, options))
            try:
                yield json.dumps({'type': 'complete', 'session': {
                    'id': 'archived', 'segments': [{'start': 1, 'end': 2, 'raw': '重要'}]}}).encode()
            finally:
                closed.append(True)
        with patch.dict(sys.modules, {'app': SimpleNamespace(batch_transcription_stream=stream)}), \
             patch('qwen_asr_app.inference_providers.inference_settings.for_new_task',
                   return_value={'asr_provider': 'remote_only'}) as settings:
            segments, ident = await transcribe_audio(self.image, '课程')
        self.assertEqual(ident, 'archived')
        self.assertEqual(segments[0]['raw'], '重要')
        settings.assert_called_once_with('asr')
        self.assertEqual(calls[0][2]['category'], '课程')
        self.assertEqual(calls[0][2]['inference_settings']['asr_provider'], 'remote_only')
        self.assertIn('_remote_operation_dir', calls[0][2])
        self.assertEqual(calls[0][3], {'preserve_source': True})
        self.assertEqual(closed, [True])

    async def test_cli_asr_failure_still_writes_pdf_and_warning_manifest(self):
        output, manifest = self.root / 'cli.pdf', self.root / 'cli.json'
        errors = io.StringIO()
        with patch.object(sys, 'argv', ['extract_vga_slides.py', 'video.mp4', '--audio', str(self.image),
                                       '-o', str(output), '--manifest', str(manifest)]), \
             patch('scripts.extract_vga_slides.extract_slides', return_value=[self.result().pages[0].slide]), \
             patch('qwen_asr_app.vga_emphasis.transcribe_audio', new_callable=AsyncMock,
                   side_effect=RuntimeError('语音识别暂不可用')), \
             patch.dict(sys.modules, {'app': SimpleNamespace(model_manager=SimpleNamespace(close=AsyncMock()))}), \
             contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            await asyncio.to_thread(cli_main)
        self.assertTrue(output.is_file())
        saved = json.loads(manifest.read_text())
        self.assertEqual(len(saved['pages']), 1)
        self.assertFalse(saved['pages'][0]['important'])
        self.assertIn('语音识别暂不可用', saved['warnings'][0])
        self.assertIn('警告：', errors.getvalue())

    async def test_running_transcription_is_cancelled(self):
        entered, cancelled, stop = asyncio.Event(), asyncio.Event(), Event()
        async def transcribe(*args):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        with patch('qwen_asr_app.vga_emphasis.find_audio', return_value=self.image), \
             patch('qwen_asr_app.vga_emphasis.transcribe_audio', side_effect=transcribe):
            task = asyncio.create_task(apply_emphasis(self.result(), self.image, cancel=stop.is_set))
            await asyncio.wait_for(entered.wait(), 2)
            stop.set()
            with self.assertRaises(ExtractionCancelled):
                await task
        self.assertTrue(cancelled.is_set())
