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
        self.root = Path(temporary.name).resolve()
        self.image = self.root / 'page.png'
        fixture('document').save(self.image)

    def result(self):
        return filter_slides([Slide(i, start, start + 10, start + 9, self.image)
                              for i, start in enumerate((0, 100), 1)])

    def test_overlap_duplicate_windows_keywords_and_overlay(self):
        result = self.result()
        self.assertEqual(len(result.pages), 1)
        segments = [{'start': 10, 'end': 99, 'raw': '普通但不重叠'},
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
        self.assertGreater(manifest['pages'][0]['emphasis_score'], 0)
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

    def test_density_ranking_cap_and_rounding(self):
        pages = [SimpleNamespace(occurrences=[{'start_sec': i * 1000, 'end_sec': i * 1000 + 600}])
                 for i in range(8)]
        pages[0].occurrences = [{'start_sec': 0, 'end_sec': 5},
                                {'start_sec': 100, 'end_sec': 215}]
        pages[1].occurrences[0]['end_sec'] = 1001
        result = SimpleNamespace(pages=pages)
        segments = [{'start': i * 1000 + .1, 'end': i * 1000 + .5, 'raw': '重点' * (i + 1)}
                    for i in range(8)]
        mark_emphasis(result, segments)
        self.assertEqual([i for i, p in enumerate(pages) if p.important], [1, 7])
        self.assertEqual(pages[0].emphasis_score, .5)
        self.assertEqual(pages[1].emphasis_score, 2)
        self.assertEqual(len(pages), 8)

    def test_long_segment_assigns_hit_once_and_centers_snippet(self):
        pages = [SimpleNamespace(occurrences=[{'start_sec': i, 'end_sec': i + 1}])
                 for i in range(90)]
        raw = '前' * 100 + '考试' + '后' * 100
        mark_emphasis(SimpleNamespace(pages=pages), [{'start': 0, 'end': 90, 'raw': raw}])
        self.assertEqual([i for i, p in enumerate(pages) if p.important], [45])
        self.assertEqual(sum(p.emphasis_score for p in pages), 1)
        snippet = pages[45].matched_snippets[0]
        self.assertEqual(snippet['hit_sec'], 45)
        self.assertEqual(snippet['text'], '前' * 40 + '考试' + '后' * 40)

    def test_hit_uses_nearest_page_and_revisit_without_double_counting(self):
        pages = [SimpleNamespace(occurrences=[{'start_sec': 0, 'end_sec': 10},
                                              {'start_sec': 80, 'end_sec': 90}]),
                 SimpleNamespace(occurrences=[{'start_sec': 40, 'end_sec': 50}])]
        result = SimpleNamespace(pages=pages)
        mark_emphasis(result, [{'start': 70, 'end': 72, 'raw': '重点'},
                               {'start': 84, 'end': 86, 'raw': '考试'}])
        self.assertEqual(pages[0].emphasis_score, 2)
        self.assertEqual(pages[1].emphasis_score, 0)
        self.assertEqual(len(pages[0].matched_snippets), 2)
        pages[1].occurrences = [{'start_sec': 80, 'end_sec': 90}]
        mark_emphasis(result, [{'start': 84, 'end': 86, 'raw': '考试'}])
        self.assertEqual(sum(p.emphasis_score for p in pages), 1)

    def test_weak_words_no_hits_empty_and_explicit_emphasis(self):
        result = self.result()
        page = result.pages[0]
        for text in ('注意，一定要记住，掌握关键，很重要，容易易错', '', '普通讲解'):
            mark_emphasis(result, [{'start': 1, 'end': 2, 'raw': text}])
            self.assertFalse(page.important)
            self.assertEqual(page.emphasis_score, 0)
            self.assertEqual(page.matched_snippets, [])
        for text in ('这个重要', '这里很重要', '这一点特别重要'):
            mark_emphasis(result, [{'start': 1, 'end': 2, 'raw': text}])
            self.assertTrue(page.important)
        mark_emphasis(result, [])
        self.assertFalse(page.important)
        self.assertEqual(page.emphasis_score, 0)
        self.assertEqual(mark_emphasis(SimpleNamespace(pages=[]), []).pages, [])

    def test_sibling_explicit_and_video_audio_detection(self):
        video = self.root / 'lecture.mp4'
        synthetic_video(video)
        with self.assertRaisesRegex(ValueError, '没有音轨'):
            find_audio(video)
        for extension in ('.wav', '.mp3', '.m4a', '.aac'):
            sibling = video.with_suffix(extension)
            sibling.touch()
            self.assertEqual(find_audio(video), sibling)
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(find_audio(alias / video.name), video.with_suffix('.aac'))
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

    async def test_filter_disabled_cli_retains_revisit_emphasis(self):
        video = self.root / 'lecture.mp4'
        synthetic_video(video)
        slides = extract_slides(video)
        self.assertEqual(len(slides), 3)
        self.assertEqual(len(slides[0].occurrences), 2)
        revisit = slides[0].occurrences[-1]
        transcript = self.root / 'transcript.json'
        transcript.write_text(json.dumps([{'start': revisit['start_sec'] + .1,
                                          'end': revisit['end_sec'], 'raw': '考试重点'}]))
        output, manifest = self.root / 'revisit.pdf', self.root / 'revisit.json'
        with patch.object(sys, 'argv', ['extract_vga_slides.py', str(video), '--no-filter',
                                       '--transcript', str(transcript), '-o', str(output),
                                       '--manifest', str(manifest)]), contextlib.redirect_stdout(io.StringIO()):
            await asyncio.to_thread(cli_main)
        saved = json.loads(manifest.read_text())
        self.assertEqual(len(saved['pages']), 3)
        self.assertTrue(saved['pages'][0]['important'])
        self.assertEqual(len(saved['pages'][0]['occurrences']), 2)

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

    async def test_transcription_payload_cleanup_preserves_archive_and_source(self):
        archive = self.root / 'course' / 'recording.aac'
        archive.parent.mkdir()
        archive.write_bytes(b'archived audio')
        folders = []
        mode = 'complete'
        async def stream(path, name, config, **options):
            folder = Path(config['_remote_operation_dir'])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'audio.wav').write_bytes(b'large PCM audio')
            downloaded = folder / 'downloads'
            downloaded.mkdir()
            (downloaded / 'segments.json').write_text('[]')
            folders.append(folder.parent)
            if mode == 'raise':
                raise OSError('远程连接失败')
            if mode == 'error':
                yield json.dumps({'type': 'error', 'message': '转写失败'}).encode()
            else:
                yield json.dumps({'type': 'complete', 'session': {'id': 'archive', 'segments': []}}).encode()
        with patch.dict(sys.modules, {'app': SimpleNamespace(batch_transcription_stream=stream)}), \
             patch('qwen_asr_app.vga_emphasis.PRIVATE_ROOT', self.root / 'private'), \
             patch('qwen_asr_app.remote_operations.PRIVATE_ROOT', self.root / 'private'), \
             patch('qwen_asr_app.inference_providers.inference_settings.for_new_task',
                   return_value={'asr_provider': 'remote_only'}):
            for mode in ('complete', 'error', 'raise'):
                if mode == 'complete':
                    self.assertEqual((await transcribe_audio(self.image, '课程'))[1], 'archive')
                else:
                    with self.assertRaises((RuntimeError, OSError)):
                        await transcribe_audio(self.image, '课程')
                self.assertFalse(folders[-1].exists())
                self.assertEqual(archive.read_bytes(), b'archived audio')
                self.assertTrue(self.image.exists())

    async def test_failed_poll_preserves_recovery_receipt_but_discards_payloads(self):
        folders = []
        state = 'running'
        async def stream(path, name, config, **options):
            folder = Path(config['_remote_operation_dir'])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'receipt.json').write_text(json.dumps({
                'state': state, 'job_id': 'accepted-job', 'key': 'idempotency-key',
                'fingerprint': {'kind': 'asr', 'endpoint': 'http://localhost:8765'}}))
            (folder / 'receipt.cancel.json').write_text('{"requested": true}')
            (folder / 'receipt.lock').touch()
            (folder / 'audio.wav').write_bytes(b'PCM')
            (folder / 'input.wav').write_bytes(b'canonical PCM')
            downloads = folder / 'downloads'
            downloads.mkdir()
            (downloads / 'segments.json').write_text('[]')
            (folder / 'downloaded-result.zip').write_bytes(b'zip')
            folders.append(folder)
            yield json.dumps({'type': 'error', 'message': '远程轮询失败，取消尚未确认'}).encode()
        with patch.dict(sys.modules, {'app': SimpleNamespace(batch_transcription_stream=stream)}), \
             patch('qwen_asr_app.vga_emphasis.PRIVATE_ROOT', self.root / 'private'), \
             patch('qwen_asr_app.remote_operations.PRIVATE_ROOT', self.root / 'private'), \
             patch('qwen_asr_app.inference_providers.inference_settings.for_new_task',
                   return_value={'asr_provider': 'remote_only'}):
            for state in ('running', 'cancel_unconfirmed'):
                with self.assertRaisesRegex(RuntimeError, '远程轮询失败'):
                    await transcribe_audio(self.image, '课程')
                folder = folders[-1]
                receipt = json.loads((folder / 'receipt.json').read_text())
                self.assertEqual(receipt['state'], state)
                self.assertEqual(receipt['job_id'], 'accepted-job')
                self.assertEqual(receipt['key'], 'idempotency-key')
                self.assertEqual({p.name for p in folder.iterdir()},
                                 {'receipt.json', 'receipt.cancel.json', 'receipt.lock'})
                self.assertTrue(self.image.is_file())

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
