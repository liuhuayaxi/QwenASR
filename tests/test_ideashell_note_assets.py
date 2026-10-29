from tests import isolation  # noqa: F401

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import app
from qwen_asr_app.ideashell_note_assets import prepare_push_assets, restore_image_markdown
from qwen_asr_app.note_content import compose_push, summary_from_archive
from qwen_asr_app.storage import RecordingRecord


ITEM = {'type': 'array', 'maxItems': 9, 'items': {'type': 'object',
        'properties': {'name': {'type': 'string'}, 'data': {'type': 'string'}},
        'required': ['name', 'data']}}
SCHEMA = {'properties': {'images': ITEM, 'documents': {**ITEM, 'maxItems': 5}}}


def illustrated_record(root, count=20):
    folder = root / '.review-notes' / ('a' * 24)
    (folder / 'assets').mkdir(parents=True)
    lines = ['# 图文笔记']
    for index in range(count):
        name = f'fig-{index:016x}.png'
        Image.new('RGB', (10, 10), (index, 20, 30)).save(folder / 'assets' / name)
        lines.append(f'![示意图 {index + 1}](assets/{name})')
    record = RecordingRecord(root, {'id': 'image-note', 'category': '测试课程', 'title': '图文课次',
        'status': 'completed', 'ai_note_summary': {'summary': '\n\n'.join(lines),
        'review_package': '.review-notes/' + 'a' * 24}})
    record.segments = [{'id': 1, 'start': 0, 'end': 1, 'raw': '原文'}]
    return record, folder


class NoteAssetsTests(unittest.TestCase):
    def test_images_are_real_bytes_pdf_not_uploaded_and_pull_restores_local_illustrations(self):
        with tempfile.TemporaryDirectory() as raw:
            record, folder = illustrated_record(Path(raw), 2)
            note = record.metadata['ai_note_summary']
            content = note['summary']
            data = b'%PDF-published-note'
            note.update(pdf_sha256=hashlib.sha256(data).hexdigest(), review_status='passed')
            exported = folder / 'pdf' / 'version'
            exported.mkdir(parents=True)
            (exported / 'candidate.pdf').write_bytes(data)
            (exported / 'export.json').write_text(json.dumps({'pdf_sha256': note['pdf_sha256'],
                'body_sha256': hashlib.sha256(content.encode()).hexdigest()}))
            archive, images, documents, bindings = prepare_push_assets(record, compose_push(record, 'summary'))
            self.assertNotIn('](assets/', archive['content'])
            self.assertEqual(len(images), 2)
            self.assertTrue(all(i['data'].startswith(b'\x89PNG') for i in images))
            self.assertEqual(documents, [])  # the review PDF is never uploaded
            self.assertNotIn('data', archive['images'][0])
            restored = restore_image_markdown(summary_from_archive(archive['content'], {'content_mode': 'summary'}), bindings)
            self.assertEqual(restored, content)
            self.assertEqual(note['summary'], content)
            old = archive['assets_digest']
            Image.new('RGB', (10, 10), 'blue').save(folder / 'assets' / 'fig-0000000000000000.png')
            current, *_ = prepare_push_assets(record, compose_push(record, 'summary'))
            self.assertNotEqual(old, current['assets_digest'])

    def test_missing_or_escaping_image_blocks_push_and_stale_pdf_is_not_attached(self):
        with tempfile.TemporaryDirectory() as raw:
            record, folder = illustrated_record(Path(raw), 1)
            record.metadata['ai_note_summary'].update(review_status='passed', pdf_sha256='old')
            archive, _, documents, _ = prepare_push_assets(record, compose_push(record, 'summary'))
            self.assertEqual(documents, [])
            self.assertEqual(archive['pdf_warning'], '')
            (folder / 'assets' / 'fig-0000000000000000.png').unlink()
            with self.assertRaisesRegex(ValueError, '图片不存在'):
                prepare_push_assets(record, compose_push(record, 'summary'))
            record.metadata['ai_note_summary']['summary'] = '![escape](../session.json)'
            with self.assertRaises(ValueError):
                prepare_push_assets(record, compose_push(record, 'summary'))


class PushAssetsTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_images_share_one_note_with_partial_progress_and_safe_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            record, _ = illustrated_record(Path(raw))
            failure = 3  # survives the in-request retries
            async def remote(name, arguments):
                nonlocal failure
                if name == 'note_create':
                    self.assertEqual(len(arguments['images']), 9)
                    return {'note_id': 'b' * 32}
                if name == 'note_update' and failure:
                    failure -= 1
                    raise RuntimeError('上传中断')
                return {}
            connection = SimpleNamespace(server_url='https://mcp.ideashell.cn/mcp',
                list_tools=AsyncMock(return_value={'note_create': SCHEMA, 'note_update': SCHEMA}),
                call_tool=AsyncMock(side_effect=remote))
            with patch.object(app.recording_store, 'find', return_value=record), \
                 patch.object(app, 'ideashell_recording_audio_parts', new=AsyncMock(return_value=[])), \
                 patch.object(app.ideashell_manager, 'connection', return_value=connection), \
                 patch.object(app, 'ensure_ideashell_course_folder', new=AsyncMock(return_value='c' * 32)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                    preview = (await client.post('/api/recordings/image-note/ideashell/preview', json={'content_mode': 'summary'})).json()
                    body = {'content_mode': 'summary', 'preview_digest': preview['digest'], 'assets_digest': preview['assets_digest']}
                    missing_manifest = await client.post('/api/recordings/image-note/ideashell/push', json={'content_mode': 'summary'})
                    self.assertEqual(missing_manifest.status_code, 400)
                    self.assertEqual(connection.call_tool.call_count, 0)
                    interrupted = await client.post('/api/recordings/image-note/ideashell/push', json=body)
                    self.assertEqual(interrupted.status_code, 400)
                    self.assertEqual(record.metadata['ideashell_sync']['note_id'], 'b' * 32)
                    self.assertEqual(record.metadata['ideashell_sync']['status'], 'partial')
                    self.assertEqual(record.metadata['ideashell_sync']['images_uploaded'], 9)
                    retry = await client.post('/api/recordings/image-note/ideashell/push', json=body)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(retry.json()['images_attached'], 20)
            self.assertEqual(record.metadata['ideashell_sync']['images_uploaded'], 20)
            self.assertEqual(record.metadata['ideashell_sync']['status'], 'synced')
            calls = connection.call_tool.call_args_list
            self.assertEqual(sum(call.args[0] == 'note_create' for call in calls), 1)
            batches = [call.args[1]['images'] for call in calls if call.args[0] == 'note_update']
            # Three failed attempts at batch 2, then the retry resumes with only
            # the missing images and never re-sends the text (no extra memo).
            self.assertEqual([len(items) for items in batches], [9, 9, 9, 9, 2])
            self.assertFalse(any('content' in call.args[1] for call in calls if call.args[0] == 'note_update'))

    async def test_changed_figure_bytes_require_a_fresh_preview(self):
        with tempfile.TemporaryDirectory() as raw:
            record, folder = illustrated_record(Path(raw), 1)
            archive, *_ = prepare_push_assets(record, compose_push(record, 'summary'))
            Image.new('RGB', (10, 10), 'blue').save(folder / 'assets' / 'fig-0000000000000000.png')
            with patch.object(app.recording_store, 'find', return_value=record), \
                 patch.object(app, 'ideashell_recording_audio_parts', new=AsyncMock(return_value=[])), \
                 patch.object(app.ideashell_manager, 'connection') as connect:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
                    response = await client.post('/api/recordings/image-note/ideashell/push', json={
                        'content_mode': 'summary', 'preview_digest': archive['digest'], 'assets_digest': archive['assets_digest']})
            self.assertEqual(response.status_code, 400)
            connect.assert_not_called()
