from tests import isolation  # noqa: F401
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from qwen_asr_app.chapter_routes import chapter_router, apply_manual_chapter
from qwen_asr_app.chapters import chapter_fields
from qwen_asr_app.library_query import listing
from qwen_asr_app.library_search import LibrarySearch
from qwen_asr_app.note_pdf import chapter_provenance, fingerprint
from qwen_asr_app.note_latex import prepare_document
from qwen_asr_app.storage import RecordingRecord


class ChapterQueryTests(unittest.TestCase):
    def test_cross_chapter_filters_counts_unknown_date_and_pagination(self):
        records = [dict(id=str(i), category='光学', started_at=f'2026-10-{i+1:02d}T08:00:00+08:00',
                        has_note=True, chapters=labels) for i,labels in enumerate([
                            ['第2章 干涉'], ['第2章 干涉', '第3章 衍射'], [], ['第3章 衍射']])]
        records.append(dict(id='other', category='通信', has_note=True, chapters=['第2章 干涉']))
        result = listing(records, '光学', offset=1, limit=1, chapter='第2章 干涉')
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['records'][0]['id'], '0')
        counts = {row['chapter']: row['notes'] for row in result['chapter_counts']}
        self.assertEqual(counts, {'第2章 干涉':2, '第3章 衍射':2, '__unassigned__':1})
        self.assertEqual(listing(records,'光学',chapter='__unassigned__')['records'][0]['id'],'2')
        self.assertEqual(listing(records,'光学',date='2026-10-02',chapter='第3章 衍射')['total'],1)

    def test_search_honors_chapter_and_date_and_searches_chapter_text(self):
        with tempfile.TemporaryDirectory() as temp:
            records=[]
            for i,label in enumerate(['第2章 干涉','第3章 衍射']):
                folder=Path(temp)/str(i);folder.mkdir()
                rec=RecordingRecord(folder,dict(id=str(i),category='光学',title='课堂',status='completed',
                    started_at=f'2026-10-0{i+1}T08:00:00+08:00',chapters=[label],ai_note_summary={'summary':'光学条件'}))
                rec.save_metadata();records.append(rec)
            search=LibrarySearch()
            self.assertEqual(search.search(records,'光学',chapter='第3章 衍射')['total'],1)
            self.assertEqual(search.search(records,'光学',chapter='第3章 衍射',date='2026-10-01')['total'],0)
            self.assertEqual(search.search(records,'干涉')['matches'][0]['recording_id'],'0')


class ChapterMetadataTests(unittest.TestCase):
    def test_manual_clear_is_preserved_and_note_body_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            record=RecordingRecord(Path(temp),dict(id='example',status='completed',title='课堂',
                ai_note_summary={'summary':'# 原来的正文\n\n不改动。'}))
            apply_manual_chapter(record,'第3章 激光；第4章 光放大')
            self.assertEqual(record.list_item()['chapters'],['第3章 激光','第4章 光放大'])
            apply_manual_chapter(record,'')
            saved=json.loads(record.meta_path.read_text())
            self.assertEqual(saved['chapter_info']['source'],'manual')
            self.assertEqual(saved['ai_note_summary']['summary'],'# 原来的正文\n\n不改动。')

    def test_suggestion_is_preview_and_bulk_preserves_manual(self):
        with tempfile.TemporaryDirectory() as temp:
            record=RecordingRecord(Path(temp),dict(id='example',status='completed',title='课堂',
                ai_note_summary={'summary':'# 正文'}))
            record.save_metadata()
            store=SimpleNamespace(find=lambda ident:record,iter_list_items=lambda:iter([record.list_item()]))
            app=FastAPI();app.include_router(chapter_router(store,lambda f:f))
            client=TestClient(app)
            suggestion=chapter_fields(['第2章 光学'],'materials',[{'source_name':'第2章.pdf','page':1,'text':'第二章 光学'}])
            with patch('qwen_asr_app.chapter_routes.suggestion_for_record',return_value=suggestion):
                response=client.post('/api/recordings/example/chapter-suggestion')
                self.assertEqual(response.status_code,200)
                self.assertNotIn('chapter',record.metadata)
                result=client.post('/api/library/chapters/detect',json={}).json()
                self.assertEqual(result['updated'],1)
                apply_manual_chapter(record,'')
                result=client.post('/api/library/chapters/detect',json={}).json()
                self.assertEqual(result['unchanged'],1)
                self.assertEqual(record.metadata['chapters'],[])

    def test_chapter_change_invalidates_pdf_fingerprint_without_body_change(self):
        with tempfile.TemporaryDirectory() as temp:
            text='# 正文\n\n公式';base={'layout_engine':'chromium-markdown-katex-v1'}
            old=fingerprint(text,temp,base)
            new=chapter_provenance(base,{'chapters':['第2章 光学']})
            self.assertNotEqual(old,fingerprint(text,temp,new))
            self.assertEqual(chapter_provenance(new,{'chapters':[]}),base)

    def test_latex_subtitle_is_escaped_and_preserves_source_body(self):
        with tempfile.TemporaryDirectory() as temp:
            body=r'\CourseTitle{课堂}{课程与日期}'+'\n正文。'
            result=prepare_document(body,temp,transcript={'blocks':[]},images=[],chapters=['第2章 A&B 50%'])
            self.assertIn(r'第2章 A\&B 50\%',result['source'])
            self.assertIn(body.splitlines()[0],result['source'])


if __name__=='__main__': unittest.main()
