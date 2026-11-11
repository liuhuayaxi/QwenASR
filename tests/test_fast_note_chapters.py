"""Exercise automatic chapter persistence through the actual fast-note pipeline."""
from tests import isolation  # noqa: F401

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from qwen_asr_app import fast_note
from qwen_asr_app.chapter_routes import apply_manual_chapter
from qwen_asr_app.storage import RecordingRecord


class FastNoteChapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = RecordingRecord(Path(temporary.name), {
            'id': 'chapter-note', 'title': '光探测器课堂', 'category': '光电子学',
            'status': 'completed', 'started_at': '2026-11-02T09:50:00+08:00',
            'review_material_ids': ['slides'],
        })
        self.record.segments = [{'id': 1, 'start': 0, 'end': 20, 'raw': '光电探测器与半导体光吸收。'}]
        self.record.save_all()
        self.package = {
            'category': '光电子学', 'lesson_date': '2026-11-02T09:50:00+08:00',
            'sources': [{'source_id': 'slides', 'name': '第六章 光探测器.pdf', 'sha256': 'original',
                         'pages': [{'page': 1, 'text': '第六章 光探测器'}, {'page': 2, 'text': '半导体光吸收'}]}],
            'relevant_pages': [{'source_id': 'slides', 'page': 2, 'score': 10}],
            'material_selection': {'files': [{'source_id': 'slides', 'chosen': True, 'mode': 'manual'}]},
        }

        def collect(record, folder, selection):
            (folder / 'materials.json').write_text(json.dumps(self.package), encoding='utf-8')
            return copy.deepcopy(self.package)

        self.writer = AsyncMock(return_value='## 光吸收与探测\n\n半导体吸收入射光并产生光生载流子。')
        self.export = Mock(return_value=(Path('synthetic.pdf'), {'pdf_sha256': 'synthetic'}))
        self.collect = Mock(side_effect=collect)
        for context in (
            patch('qwen_asr_app.course_files.CourseFiles.list', return_value=[
                {'id': 'slides', 'name': '课件.pdf', 'sha256': 'original'}]),
            patch('qwen_asr_app.batch_ai._yuketang_questions', AsyncMock(return_value=[])),
            patch.object(fast_note, 'collect', self.collect),
            patch.object(fast_note.review_scope, 'prepare', AsyncMock(return_value={'enabled': False})),
            patch.object(fast_note, 'request_ai_text', self.writer),
            patch('qwen_asr_app.note_pdf.export_pdf', self.export),
            patch('qwen_asr_app.review_notes.disabled_review', return_value={'status': 'skipped'}),
            patch('qwen_asr_app.chapters._course_originals', side_effect=AssertionError('must reuse lesson package')),
        ):
            context.start()
            self.addCleanup(context.stop)

    async def test_new_note_persists_chapter_before_pdf_export_and_publication(self):
        segments = copy.deepcopy(self.record.segments)
        note = await fast_note.generate_fast(self.record, {})
        saved = json.loads(self.record.meta_path.read_text())
        self.assertEqual(saved['chapters'], ['第6章 光探测器'])
        self.assertEqual(saved['chapter_info']['source'], 'materials')
        self.assertEqual(saved['chapter_info']['evidence'][0]['page'], 1)
        self.assertEqual(self.record.list_item()['chapters'], saved['chapters'])
        self.assertEqual(self.export.call_args.args[2]['chapter_labels'], saved['chapters'])
        self.assertEqual(note['material_version']['chapter_labels'], saved['chapters'])
        self.assertEqual(self.record.segments, segments)
        self.assertEqual(note['review_policy'], 'disabled_by_settings')
        self.writer.assert_awaited_once()

    async def test_material_only_complete_coverage_also_assigns_chapter(self):
        self.record.segments = []
        self.record.save_all()
        note = await fast_note.generate_fast(self.record, {'_note_material_coverage': 'selected_files'})
        self.assertEqual(self.record.metadata['chapters'], ['第6章 光探测器'])
        self.assertEqual(note['material_version']['chapter_labels'], ['第6章 光探测器'])
        self.assertEqual(note['material_version']['material_coverage'], 'selected_files')
        self.assertEqual(self.record.segments, [])

    async def test_manual_chapter_is_preserved_in_metadata_and_pdf(self):
        apply_manual_chapter(self.record, '第六章 自定章节名称')
        await fast_note.generate_fast(self.record, {})
        self.assertEqual(self.record.metadata['chapter_info']['source'], 'manual')
        self.assertEqual(self.record.metadata['chapters'], ['第六章 自定章节名称'])
        self.assertEqual(self.export.call_args.args[2]['chapter_labels'], ['第六章 自定章节名称'])

    async def test_manual_clear_is_preserved(self):
        apply_manual_chapter(self.record, '')
        await fast_note.generate_fast(self.record, {})
        self.assertEqual(self.record.metadata['chapter_info']['source'], 'manual')
        self.assertEqual(self.record.metadata['chapters'], [])
        self.assertNotIn('chapter_labels', self.export.call_args.args[2])

    async def test_ambiguous_book_without_page_evidence_stays_unassigned(self):
        self.package['sources'][0].update(name='完整教材.pdf', pages=[
            {'page': 1, 'text': '第五章 光放大器'}, {'page': 20, 'text': '第六章 光探测器'}])
        self.package['relevant_pages'] = []
        await fast_note.generate_fast(self.record, {})
        self.assertNotIn('chapters', self.record.metadata)
        self.assertNotIn('chapter_labels', self.export.call_args.args[2])

    async def test_completed_note_reuse_repairs_metadata_without_regeneration(self):
        await fast_note.generate_fast(self.record, {})
        # Emulate a note published by the old fast pipeline, with no chapters.
        for key in ('chapter', 'chapters', 'chapter_info'):
            self.record.metadata.pop(key, None)
        note = self.record.metadata['ai_note_summary']
        note['material_version'].pop('chapter_labels', None)
        self.record.metadata['ideashell_sync'] = {'note_id': 'existing-note', 'status': 'synced'}
        self.record.save_metadata()
        before = {str(p): p.read_bytes() for p in self.record.directory.rglob('*')
                  if p.is_file() and p != self.record.meta_path}
        self.writer.reset_mock(); self.collect.reset_mock(); self.export.reset_mock()
        self.assertEqual(await fast_note.generate_fast(self.record, {}), note)
        self.assertEqual(self.record.metadata['chapters'], ['第6章 光探测器'])
        self.assertEqual(self.record.metadata['ideashell_sync']['status'], 'local_changes')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.record.directory.rglob('*')
                                 if p.is_file() and p != self.record.meta_path})
        self.writer.assert_not_awaited(); self.collect.assert_not_called(); self.export.assert_not_called()

    async def test_explicit_regeneration_writes_a_new_package_but_resume_reuses_that_round(self):
        old=await fast_note.generate_fast(self.record,{})
        new=await fast_note.generate_fast(self.record,{'_note_generation_id':'explicit-user-round-1'})
        self.assertNotEqual(old['review_package'],new['review_package'])
        self.assertTrue((self.record.directory/old['review_package']/'writer.md').is_file())
        self.assertEqual(self.writer.await_count,2)
        resumed=await fast_note.generate_fast(self.record,{'_note_generation_id':'explicit-user-round-1'})
        self.assertEqual(resumed['review_package'],new['review_package'])
        self.assertEqual(self.writer.await_count,2)
        again=await fast_note.generate_fast(self.record,{'_note_generation_id':'explicit-user-round-2'})
        self.assertNotEqual(again['review_package'],new['review_package'])
        self.assertEqual(self.writer.await_count,3)


if __name__ == '__main__':
    unittest.main()
