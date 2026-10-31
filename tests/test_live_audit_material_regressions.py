from tests import isolation  # noqa: F401

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import pymupdf as fitz

from qwen_asr_app import review_scope
from qwen_asr_app.review_materials import learning_scope_candidates, native_pages
from qwen_asr_app.review_notes import validate_plan
from scripts.build_ai_friendly_pdf import add_cover


class TextMaterialTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_evidence_does_not_request_a_fabricated_slide_image(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            for extension in ('txt', 'md'):
                path = folder / ('requirements.' + extension)
                text = '学习要求：掌握欧姆定律 V = I R。'
                path.write_text(text, encoding='utf-8')
                source = {'source_id': 'text', 'path': path.name, 'name': path.name,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'pages': native_pages(path)}
                self.assertEqual(source['pages'][0]['text'], text)
                self.assertEqual(learning_scope_candidates([source]), [])
                # A task saved before this fix can still contain this candidate.
                package = {'category': '测试课程', 'sources': [source],
                           'learning_scope_candidates': [{'source_id': 'text', 'page': 1}]}
                with patch.object(review_scope, 'extract_image') as render, \
                     patch.object(review_scope, 'request_materials', AsyncMock()) as request:
                    actual = await review_scope.prepare(package, folder, {})
                self.assertFalse(actual['enabled'])
                render.assert_not_called()
                request.assert_not_awaited()
                self.assertEqual(package['sources'][0]['pages'][0]['text'], text)

    def test_visual_planning_rejects_text_images_but_retains_text_knowledge(self):
        package = {'sources': [{'source_id': 'text', 'path': 'notes.md', 'pages': [{}]}]}
        plan = {'knowledge': [{'kind': 'material_supplement', 'sources': []}], 'figures': []}
        self.assertIs(validate_plan(plan, set(), package), plan)
        plan['figures'] = [{'source_id': 'text', 'page': 1}]
        with self.assertRaisesRegex(ValueError, '纯文本资料'):
            validate_plan(plan, set(), package)


class CoverLayoutTests(unittest.TestCase):
    def test_small_and_landscape_source_sizes_keep_all_cover_text_inside_page(self):
        with tempfile.TemporaryDirectory() as raw:
            font = Path(raw) / 'unicode.ttf'
            font.write_bytes(fitz.Font('cjk').buffer)
            for size in ((100, 100), (400, 300), (595, 842), (842, 595)):
                with self.subTest(size=size), fitz.open() as doc:
                    add_cover(doc, '浏览器实测资料标题' * 10, 'source-long-name.pdf',
                              {'page_size': size, 'page_count': 1,
                               'item_count': 2, 'equation_count': 0}, font)
                    page = doc[0]
                    text = page.get_text()
                    self.assertIn('AI-readable PDF', text)
                    self.assertIn('AI_TRANSCRIPT_PAGE:n', text)
                    self.assertIn('the original page image.', ' '.join(text.split()))
                    self.assertIn('source-long-name.pdf', text)
                    for block in page.get_text('dict')['blocks']:
                        for line in block.get('lines', []):
                            for span in line['spans']:
                                self.assertTrue(page.rect.contains(fitz.Rect(span['bbox'])), span)


if __name__ == '__main__':
    unittest.main()
