"""Policy regressions on synthetic artifacts; not MinerU quality evaluations."""
import tests.isolation  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf

from tests.test_knowledge import FakeEmbedder, MockCourseFiles, MockRecordingStore
from qwen_asr_app.knowledge.service import KnowledgeService
from qwen_asr_app.knowledge.task_adapter import IntelligenceTasks


class ConvertedPDFIndexTests(unittest.TestCase):
    def test_default_small_model_has_distinct_contract_from_large_model(self):
        from qwen_asr_app.knowledge.settings import KnowledgeSettings, embedding_contract
        small = embedding_contract(KnowledgeSettings.validate({}))
        large = embedding_contract(KnowledgeSettings.validate({'model_id':'Qwen/Qwen3-Embedding-4B'}))
        self.assertEqual(small['model_id'], 'Qwen/Qwen3-Embedding-0.6B')
        self.assertEqual(small['dimension'], 1024)
        self.assertEqual(large['dimension'], 2560)
        self.assertNotEqual(small, large)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = SimpleNamespace(storage_root=self.root, categories=['数学'])
        self.files = MockCourseFiles(self.root)
        self.records = MockRecordingStore(self.root)
        self.model = FakeEmbedder(dim=32)
        self.k = KnowledgeService(self.settings, self.records, self.files, embedder=self.model)

    def add(self, ident, text, converted=True):
        path = self.root / (ident + '.md')
        path.write_text(text)
        self.files.add_file('数学', ident, ident + '.md', '讲义', path, converted=converted)
        return path

    def test_unconverted_native_pdf_is_waiting_and_never_parsed(self):
        path = self.root / 'raw.pdf'
        with pymupdf.open() as doc:
            page = doc.new_page(); page.insert_text((40, 60), 'RAWSECRET90210')
            doc.save(path)
        self.files.add_file('数学', 'raw', 'raw.pdf', '课件', path, converted=False)
        with patch.object(self.k.ingestor, '_load_file_content', side_effect=AssertionError('raw parsing forbidden')):
            stats = self.k.sync_course('数学')
        self.assertEqual(stats['chunks_indexed'], 0)
        self.assertEqual(stats['waiting_conversion'], 1)
        self.assertEqual(self.k.search('数学', 'RAWSECRET90210'), [])
        status = self.k.status('数学')
        self.assertEqual(status['sources'][0]['state'], 'waiting_conversion')
        self.assertEqual(status['waiting_conversion_count'], 1)

    def test_conversion_completion_becomes_one_source_on_incremental_update(self):
        self.add('material', '# 定义\n\n数值孔径NA=0.20。', converted=False)
        self.k.sync_course('数学')
        self.files.convert_file('数学', 'material')
        self.k.sync_course('数学')
        sources = self.k.store.list_sources('数学', active_only=True)
        self.assertEqual([s['source_id'] for s in sources], ['cf_material_pdf'])
        self.assertEqual(self.k.status('数学')['waiting_conversion_count'], 0)
        hit = self.k.search('数学', 'NA')[0]
        self.assertEqual(hit['source_id'], 'cf_material_pdf')
        self.assertIn('/source/cf_material_pdf', hit['open_url'])
        self.assertEqual(self.k.sync_course('数学')['sources_updated'], 0)

    def test_raw_original_and_attachment_cannot_be_independent_search_sources(self):
        path = self.add('material', '转换前的独有内容 RAWONLY')
        with pymupdf.open() as doc:
            page = doc.new_page(); page.insert_text((40, 60), 'CONVERTEDONLY')
            doc.embfile_add('mineru-transcript.md', b'CONVERTEDONLY')
            doc.save(self.files.assets['数学']['material']['pdf'])
        with patch.object(self.k.ingestor, '_load_file_content', side_effect=AssertionError('raw parsing forbidden')):
            self.k.sync_course('数学')
        sources = self.k.store.list_sources('数学', active_only=True)
        self.assertEqual(len(sources), 1)
        hits = self.k.search('数学', 'CONVERTEDONLY')
        self.assertTrue(hits)
        self.assertEqual({h['source_id'] for h in hits}, {'cf_material_pdf'})
        self.assertTrue(all('RAWONLY' not in h['text'] for h in self.k.search('数学', 'RAWONLY')))

    def test_hidden_mineru_layer_excludes_native_garbage_and_has_actual_pdf_page(self):
        pdf = self.root / 'hidden.pdf'
        with pymupdf.open() as doc:
            doc.new_page()  # The converted PDF cover shifts its physical page.
            page = doc.new_page()
            page.insert_text((20, 30), 'NATIVEGARBAGE123')
            page.insert_text((20, 60), '[[AI_TRANSCRIPT_PAGE:1]]\nSOURCE_PDF_PAGE: 1\n'
                             'The following is MinerU OCR/LaTeX. The original page image is authoritative.\n'
                             '[TEXT]\nShannon capacity\n[FORMULA_LATEX]\nC = B log(1 + S/N)\n'
                             '[VISUAL_ONLY: image; inspect the original page image]\n[CAPTION] optical diagram\n'
                             '[TEXT]\nB is bandwidth.\n[[END_AI_TRANSCRIPT_PAGE:1]]')
            doc.embfile_add('mineru-transcript.md', b'ATTACHMENTDUPLICATE')
            doc.save(pdf)
        self.files.add_pdf_file('数学', 'hidden', 'hidden.pdf', '课件', pdf)
        self.k.sync_course('数学')
        hit = self.k.search('数学', 'Shannon')[0]
        self.assertEqual(hit['page'], 2)
        self.assertIn('#page=2', hit['open_url'])
        self.assertIn('$$\nC = B log(1 + S/N)\n$$', hit['text'])
        self.assertIn('$$\n\n[VISUAL_ONLY:', hit['text'])
        self.assertIn('[CAPTION]\noptical diagram', hit['text'])
        self.assertNotIn('NATIVEGARBAGE', hit['text'])
        self.assertNotIn('SOURCE_PDF_PAGE', hit['text'])
        self.assertNotIn('ATTACHMENTDUPLICATE', hit['text'])

    def test_completed_pdf_without_mineru_provenance_is_not_indexed(self):
        pdf = self.root / 'legacy.pdf'
        with pymupdf.open() as doc:
            page = doc.new_page(); page.insert_text((40, 60), 'LEGACYTEXT')
            doc.save(pdf)
        self.files.add_pdf_file('数学', 'legacy', 'legacy.pdf', '课件', pdf)
        self.files.assets['数学']['legacy']['meta']['conversion'].pop('engine')
        self.k.sync_course('数学')
        self.assertEqual(self.k.store.list_sources('数学', active_only=True), [])
        self.assertIn('未确认来自 MinerU', self.k.status('数学')['sources'][0]['error'])

    def test_incremental_update_leaves_other_source_revision_and_vectors_unchanged(self):
        class CountingModel(FakeEmbedder):
            def __init__(self): super().__init__(dim=32); self.count = 0
            def encode_documents(self, texts): self.count += len(texts); return super().encode_documents(texts)
        model = CountingModel(); self.k.embedder = model
        self.add('unchanged', '未变化的讲义NA=0.20。')
        path = self.add('changed', '# 第一段\n\n原有公式x=1。')
        self.k.sync_course('数学')
        revision = self.k.store.get_source('cf_unchanged_pdf')['active_revision']
        before = model.count
        path.write_text(path.read_text() + '\n\n# 第二段\n\n新增公式y=2。')
        self.files.convert_file('数学', 'changed')
        stats = self.k.sync_course('数学')
        self.assertEqual(stats['sources_updated'], 1)
        self.assertEqual(model.count - before, 1)
        self.assertEqual(self.k.store.get_source('cf_unchanged_pdf')['active_revision'], revision)

    def test_legacy_index_is_excluded_without_deleting_original_or_history(self):
        path = self.add('old', '原件不再独立参加索引。', converted=False)
        self.k.store.commit_source_revision(
            {'source_id': 'cf_old_orig', 'course_id': self.k.course_id('数学'), 'category': '数学',
             'title': '旧原件', 'kind': '讲义', 'origin': 'original', 'source_type': 'course_file_original',
             'file_path': str(path), 'open_url':'/api/knowledge/source/cf_old_orig'}, 'old-version', 'old-hash', 'v1', 'v1', '',
            [{'chunk_id': 'old-chunk', 'text': 'OLDINDEX', 'content_hash': 'old-chunk-hash'}], None)
        self.k.search('数学', 'OLDINDEX')
        self.assertEqual(self.k.store.get_source('cf_old_orig')['status'], 'excluded')
        self.assertEqual(self.k.store.get_chunk('old-chunk')['is_active'], 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.k.search('数学', 'OLDINDEX'), [])

    def test_deleted_waiting_material_is_no_longer_shown_as_waiting(self):
        self.add('waiting', '未转换资料', converted=False)
        self.k.sync_course('数学')
        self.files.assets['数学'].pop('waiting')
        self.k.sync_course('数学')
        self.assertEqual(self.k.status('数学')['waiting_conversion_count'], 0)
        self.assertEqual(self.k.store.get_source('cf_waiting_pdf')['status'], 'tombstoned')

    def test_knowledge_task_reports_local_execution_without_fake_asr_receipt(self):
        job = {'job_id': 'test', 'status': 'running', 'category': '数学', 'stage': '索引中',
               'progress': .25, 'created_at': '', 'updated_at': ''}
        owner = IntelligenceTasks(SimpleNamespace(jobs=lambda: [job]), None,
                                  SimpleNamespace(list_records=lambda: []))
        task = owner.tasks()[0]
        self.assertEqual(task['execution_location'], 'local')
        self.assertEqual(task['progress'], 25)
        self.assertNotIn('inference', task)
