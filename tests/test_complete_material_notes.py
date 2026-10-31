from tests import isolation  # noqa: F401
import unittest
from qwen_asr_app.fast_note import _pages, generation_identity
from types import SimpleNamespace


class CompleteMaterialNotesTests(unittest.TestCase):
    def test_edit_export_preserves_fast_pdf_layout_without_ai_reauthoring(self):
        from qwen_asr_app.review_notes import edited_note_pdf_engine, DIRECT_PDF_ENGINE, AI_LATEX_ENGINE
        self.assertEqual(edited_note_pdf_engine({'pipeline':'fast-one-turn-v6'},{'note_ai_latex_enabled':True}),DIRECT_PDF_ENGINE)
        self.assertEqual(edited_note_pdf_engine({}, {'note_ai_latex_enabled':True}),AI_LATEX_ENGINE)

    def test_complete_coverage_keeps_unranked_final_pages_and_full_text(self):
        first={'source_id':'slides','page':1,'text':'已讲内容'}
        last={'page':54,'text':'最后一页习题。'*400}
        package={'relevant_pages':[first],'sources':[{'source_id':'slides','pages':[first,last]}]}
        self.assertEqual([p['page'] for p in _pages(package)],[1])
        pages=_pages(package,complete=True)
        self.assertEqual([p['page'] for p in pages],[1,54])
        self.assertEqual(pages[-1]['text'],last['text'])

    def test_coverage_scope_has_distinct_generation_identity(self):
        record=SimpleNamespace(id='lesson')
        args=(record,[],[],['slides'],[])
        self.assertNotEqual(generation_identity(*args,{}),generation_identity(*args,{'_note_material_coverage':'selected_files'}))
