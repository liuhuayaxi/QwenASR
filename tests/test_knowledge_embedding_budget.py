import tests.isolation  # noqa: F401

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qwen_asr_app.knowledge.chunking import (
    CHUNKER_VERSION, Chunk, bound_chunks_for_embedding,
    bounded_chunker_version, chunk_markdown_text, embedding_byte_budget,
)
from qwen_asr_app.knowledge.ingest import CourseIngestor
from qwen_asr_app.knowledge.store import KnowledgeStore


class EmbeddingBudgetChunkTests(unittest.TestCase):
    def test_large_table_rows_and_repeated_headers_fit_including_multibyte_text(self):
        header = '| 知识点 | 原文与公式 |\n| :--- | ---: |'
        rows = [f'| 条目{i:03d} | 全部保留中文内容与数值 $x={i}.125e-3$。 |' for i in range(80)]
        text = '\n'.join([header, *rows])
        parent = Chunk(text, heading='本节知识点', page=13, start=1.25, end=90,
                       segment_ids=[3, 7], chunk_index=0)
        chunks, changed = bound_chunks_for_embedding([parent], 512)
        self.assertTrue(changed)
        self.assertGreater(len(chunks), 2)
        recovered = []
        for index, chunk in enumerate(chunks):
            self.assertLessEqual(len(chunk.text.encode()), embedding_byte_budget(512))
            self.assertTrue(chunk.text.startswith(header + '\n'))
            recovered.extend(chunk.text.splitlines()[2:])
            self.assertEqual((chunk.heading, chunk.page, chunk.start, chunk.end, chunk.segment_ids),
                             ('本节知识点', 13, 1.25, 90, [3, 7]))
            self.assertEqual(chunk.chunk_index, index)
        self.assertEqual(recovered, rows)

    def test_long_prose_keeps_every_character_number_inline_formula_and_provenance(self):
        sentence = 'An English sentence without Chinese punctuation explains wave propagation. '
        text = sentence * 40 + ' 数值 6.62607015e-34、-.22e-4 与公式 $E=h\\nu$ 均须保留。' + '😀中文' * 90
        parent = Chunk(text, heading='解释', start=10, end=40, segment_ids=[99])
        chunks, changed = bound_chunks_for_embedding([parent], 512)
        self.assertTrue(changed)
        self.assertEqual(re.sub(r'\s', '', ''.join(c.text for c in chunks)), re.sub(r'\s', '', text))
        self.assertTrue(all(len(c.text.encode()) <= embedding_byte_budget(512) for c in chunks))
        self.assertEqual(sum('6.62607015e-34' in c.text for c in chunks), 1)
        self.assertEqual(sum('-.22e-4' in c.text for c in chunks), 1)
        self.assertEqual(sum('$E=h\\nu$' in c.text for c in chunks), 1)
        self.assertTrue(all(c.start == 10 and c.end == 40 and c.segment_ids == [99] for c in chunks))

    def test_atomic_formula_and_wide_row_are_preserved_in_full(self):
        formula = '$$' + '\\frac{完整公式}{不许截断}' * 60 + '$$'
        wide_row = '| ' + '超长单行不可随意拆列' * 80 + ' |'
        text = '| 列名 |\n| --- |\n' + wide_row
        chunks, changed = bound_chunks_for_embedding([Chunk(formula), Chunk(text)], 512)
        self.assertIn(formula, [c.text for c in chunks])
        self.assertTrue(any(wide_row in c.text for c in chunks))
        self.assertEqual(len(chunks), 2)
        self.assertFalse(changed)


class EmbeddingBudgetIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root / 'knowledge.db')
        self.store.upsert_course('course', '数学', str(self.root))
        self.ingestor = CourseIngestor(SimpleNamespace(), None, None, self.store)
        self.contract = {'model': 'fake-budget', 'max_length': 512, 'dimension': 2}
        self.model = SimpleNamespace(contract=self.contract)
        self.calls = []

        def encode(texts):
            self.calls.extend(texts)
            self.assertTrue(all(len(t.encode()) <= embedding_byte_budget(512) for t in texts))
            return [[1.0, 0.0] for _ in texts]

        self.model.encode_documents = encode

    def spec(self, name, body):
        path = self.root / (name + '.md')
        path.write_text(body)
        return {'source_id': name, 'course_id': 'course', 'category': '数学',
                'lesson_id': 'lesson', 'title': name, 'kind': '总结', 'origin': 'ai_derived',
                'source_type': 'recording_summary', 'file_path': str(path),
                'open_url': '/?recording=lesson',
                'loader': lambda: (body, chunk_markdown_text(body))}

    def sync(self, spec):
        return self.ingestor._sync_single_source(
            spec, self.contract, json.dumps(self.contract, sort_keys=True), self.model, None)

    def test_table_recovery_is_incremental_deterministic_and_keeps_complete_text(self):
        small = self.spec('already-valid', '短而完整的原文。')
        self.sync(small)
        before = dict(self.store.get_source('already-valid'))
        with self.store._connect() as db:
            old_ids = [r[0] for r in db.execute("SELECT chunk_id FROM chunks WHERE source_id='already-valid'")]
        table_rows = [f'| 表格第{i:03d}行 | 同步保留公式 $g={i}.25$ 和中文证据。 |' for i in range(90)]
        table = '| 主题 | 证据 |\n| --- | --- |\n' + '\n'.join(table_rows)
        affected = self.spec('recovered', table)
        self.assertTrue(self.sync(affected)[0])
        calls_after_recovery = len(self.calls)
        self.assertEqual(self.sync(affected), (False, 0))
        self.assertEqual(self.sync(small), (False, 0))
        self.assertEqual(len(self.calls), calls_after_recovery)
        self.assertEqual(self.store.get_source('already-valid')['active_revision'], before['active_revision'])
        with self.store._connect() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT chunk_id FROM chunks WHERE source_id='already-valid'")], old_ids)
            source = self.store.get_source('recovered')
            version = db.execute('SELECT chunker_version FROM source_revisions WHERE source_id=? AND revision=?',
                                 ('recovered', source['active_revision'])).fetchone()[0]
            self.assertEqual(version, bounded_chunker_version(512))
            old_version = db.execute("SELECT chunker_version FROM source_revisions WHERE source_id='already-valid'").fetchone()[0]
            self.assertEqual(old_version, CHUNKER_VERSION)
            chunks = [dict(r) for r in db.execute("SELECT * FROM chunks WHERE source_id='recovered' AND is_active=1 ORDER BY rowid")]
        self.assertEqual(len({c['chunk_id'] for c in chunks}), len(chunks))
        self.assertEqual(len({c['citation_id'] for c in chunks}), len(chunks))
        self.assertEqual([row for c in chunks for row in c['text'].splitlines()[2:]], table_rows)
        self.assertTrue(all(c['open_url'] == '/?recording=lesson' and c['lesson_id'] == 'lesson' for c in chunks))
        self.assertEqual(source['needs_vectors'], 0)

    def test_unsplittable_formula_is_lexical_with_warning_and_never_sent(self):
        formula = '$$' + '\\frac{保留完整公式}{不可截断}' * 100 + '$$'
        body = '# 正文\n\n普通内容必须有向量。\n\n# 公式\n\n' + formula
        self.sync(self.spec('atomic', body))
        source = self.store.get_source('atomic')
        self.assertEqual(source['needs_vectors'], 1)
        self.assertIn('超过模型输入预算', source['error'])
        self.assertTrue(self.calls)
        self.assertTrue(all(formula not in t for t in self.calls))
        with self.store._connect() as db:
            row = db.execute("SELECT text FROM chunks WHERE source_id='atomic' AND text=?", (formula,)).fetchone()
            self.assertIsNotNone(row)
            count = db.execute('SELECT count(*) FROM chunk_vectors').fetchone()[0]
            self.assertGreater(count, 0)

    def test_windows_canonical_budget_error_does_not_abort_other_vectors(self):
        def encode(texts):
            if any('OVERSIZE' in text for text in texts):
                raise RuntimeError('Windows 暂不可用：EMBEDDING_INPUT_TOO_LONG')
            return [[1.0, 0.0] for _ in texts]
        self.model.encode_documents = encode
        body = '# 正文\n\n' + '正常原文。' * 40 + '\n\n# 原子公式\n\n$$OVERSIZE$$'
        self.assertTrue(self.sync(self.spec('remote-budget', body))[0])
        source = self.store.get_source('remote-budget')
        self.assertEqual(source['needs_vectors'], 1)
        self.assertIn('超过模型输入预算', source['error'])
        with self.store._connect() as db:
            self.assertGreater(db.execute('SELECT count(*) FROM chunk_vectors').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
