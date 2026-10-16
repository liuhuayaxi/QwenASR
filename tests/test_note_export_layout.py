"""Opt-in native Chromium/PDF acceptance; all material is synthetic."""
from tests import isolation  # noqa: F401

import json
import copy
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


class IdenticalLatexReviewTests(unittest.TestCase):
    def fixture(self,folder,scoped=True):
        old=folder/'pdf/old-adapter';old.mkdir(parents=True)
        current=folder/'pdf/new-adapter';current.mkdir()
        data=b'Identical synthetic PDF bytes; no compiler needed.'
        sha=hashlib.sha256(data).hexdigest()
        for root in (old,current):(root/'candidate.pdf').write_bytes(data)
        export={'fingerprint':'new-adapter','pdf_sha256':sha,'renderer':'xelatex-course-v1',
            'body_sha256':'markdown-sha','source_format':'latex','latex_body_sha256':'latex-body-sha',
            'template_sha256':'template-sha','pages':2,
            'source_binding':{'markdown_sha256':'markdown-sha','provenance_sha256':'provenance-sha'},
            'render':{'version':'direct-v1','body_sha256':'latex-body-sha','source_sha256':'full-source-sha',
                'template_sha256':'template-sha','assets':[{'path':'assets/fig.png','sha256':'image-sha'}],
                'source_refs':[{'source':'S1','start':1,'end':2}],
                'renderer':'direct-v1','toolchain_sha256':'toolchain-sha','pdf_sha256':sha,'pages':2}}
        review={'pdf_sha256':sha,'status':'passed','viewed_pages':[1,2],
            'policy':'visual-pages-and-originals-scope-v4' if scoped else 'visual-pages-and-originals-v3',
            'learning_scope_sha256':'scope-sha' if scoped else None,'scope_review_passed':scoped,
            'reviewer_session_ids':['independent-original-session'],'issues':[]}
        provenance={'learning_scope':{'enabled':scoped,'sha256':'scope-sha'}}
        (old/'export.json').write_text(json.dumps(export))
        (old/'review.json').write_text(json.dumps(review))
        return old,current,export,review,provenance

    def test_reuses_only_identical_bytes_and_keeps_original_review_immutable(self):
        from qwen_asr_app.note_pdf import _reuse_identical_latex_review
        for legacy in (False,True):
            with self.subTest(legacy=legacy),tempfile.TemporaryDirectory() as raw:
                folder=Path(raw);old,current,export,review,provenance=self.fixture(folder)
                if legacy:
                    previous=copy.deepcopy(export);previous.pop('source_binding')
                    (old/'export.json').write_text(json.dumps(previous))
                original=(old/'review.json').read_bytes()
                self.assertTrue(_reuse_identical_latex_review(folder,current,export,provenance))
                copied=json.loads((current/'review.json').read_text())
                self.assertEqual(copied['reviewer_session_ids'],['independent-original-session'])
                self.assertEqual(copied['viewed_pages'],[1,2])
                self.assertEqual(copied['identical_pdf_review_reuse']['source_review_sha256'],hashlib.sha256(original).hexdigest())
                self.assertEqual((old/'review.json').read_bytes(),original)

    def test_rejects_changed_source_assets_bindings_scope_or_incomplete_review(self):
        from qwen_asr_app.note_pdf import _reuse_identical_latex_review
        mutations=[('export',('body_sha256',),'different Markdown'),
            ('export',('latex_body_sha256',),'different LaTeX'),
            ('export',('template_sha256',),'different template'),
            ('export',('source_binding','provenance_sha256'),'different provenance'),
            ('export',('render','source_sha256'),'different source'),
            ('export',('render','assets'),[]),('export',('render','source_refs'),[]),
            ('export',('render','toolchain_sha256'),'different toolchain'),
            ('review',('status',),'failed'),('review',('policy',),'visual-pages-and-originals-v3'),
            ('review',('learning_scope_sha256',),'other-scope'),('review',('scope_review_passed',),False),
            ('review',('pdf_sha256',),'other-bytes'),('review',('viewed_pages',),[1]),
            ('review',('viewed_pages',),[1,1,2]),('review',('viewed_pages',),[True,2])]
        for kind,keys,value in mutations:
            with self.subTest(kind=kind,keys=keys),tempfile.TemporaryDirectory() as raw:
                folder=Path(raw);old,current,export,review,provenance=self.fixture(folder)
                changed=copy.deepcopy(export if kind=='export' else review);target=changed
                for key in keys[:-1]:target=target[key]
                target[keys[-1]]=value
                (old/(kind+'.json')).write_text(json.dumps(changed))
                self.assertFalse(_reuse_identical_latex_review(folder,current,export,provenance))
                self.assertFalse((current/'review.json').exists())

    def test_checks_actual_pdf_bytes_and_does_not_replace_newer_review(self):
        from qwen_asr_app.note_pdf import _reuse_identical_latex_review
        for corrupt in ('old','current'):
            with self.subTest(corrupt=corrupt),tempfile.TemporaryDirectory() as raw:
                folder=Path(raw);old,current,export,review,provenance=self.fixture(folder,scoped=False)
                ((old if corrupt=='old' else current)/'candidate.pdf').write_bytes(b'changed actual PDF')
                self.assertFalse(_reuse_identical_latex_review(folder,current,export,provenance))
                self.assertFalse((current/'review.json').exists())
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);old,current,export,review,provenance=self.fixture(folder,scoped=False)
            newer=b'{"status":"failed","issues":["new reviewer finding"]}'
            (current/'review.json').write_bytes(newer)
            self.assertFalse(_reuse_identical_latex_review(folder,current,export,provenance))
            self.assertEqual((current/'review.json').read_bytes(),newer)

    def test_unscoped_pass_is_reused_and_invalid_scope_or_corrupt_history_is_ignored(self):
        from qwen_asr_app.note_pdf import _reuse_identical_latex_review
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);old,current,export,review,provenance=self.fixture(folder,scoped=False)
            bad=folder/'pdf/000-corrupt';bad.mkdir()
            (bad/'export.json').write_text('not JSON');(bad/'review.json').write_text('{}')
            (bad/'candidate.pdf').write_bytes((current/'candidate.pdf').read_bytes())
            self.assertTrue(_reuse_identical_latex_review(folder,current,export,provenance))
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);old,current,export,review,provenance=self.fixture(folder)
            provenance['learning_scope'].pop('sha256')
            self.assertFalse(_reuse_identical_latex_review(folder,current,export,provenance))

    def test_concurrent_new_review_cannot_be_overwritten_during_reuse(self):
        from qwen_asr_app.note_pdf import _reuse_identical_latex_review
        from qwen_asr_app.durable import atomic_json
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);old,current,export,review,provenance=self.fixture(folder)
            newer=b'{"status":"failed","issues":["concurrent new finding"]}'
            def prepare_then_race(path,value):
                atomic_json(path,value)
                (current/'review.json').write_bytes(newer)
            with patch('qwen_asr_app.note_pdf.atomic_json',side_effect=prepare_then_race):
                self.assertFalse(_reuse_identical_latex_review(folder,current,export,provenance))
            self.assertEqual((current/'review.json').read_bytes(),newer)


@unittest.skipUnless(os.getenv('QWEN_ASR_REAL_EXPORT_TEST')=='1','native browser export is opt-in')
class NoteExportLayoutTests(unittest.TestCase):
    def test_caption_quotes_and_both_timestamp_formats_stay_with_their_content(self):
        import fitz
        from PIL import Image,ImageDraw
        from qwen_asr_app.note_pdf import export_pdf
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'assets').mkdir()
            image=Image.new('RGB',(600,280),'#e7f0f4');drawing=ImageDraw.Draw(image)
            drawing.rectangle((40,40,560,240),outline='#254f69',width=8)
            drawing.line((70,210,500,70),fill='#16775a',width=10)
            image.save(folder/'assets/fig-1234567890abcdef.png')
            markdown='''# Synthetic layout verification

<div style="height:146mm"></div>

![Synthetic diagram](assets/fig-1234567890abcdef.png)

> **图注**：FIGURE_CAPTION_ANCHOR This explicit caption belongs to the diagram.
> **读图说明**：'''+('The colored line links the two corners and the rectangle frames the complete diagram. '*5)+'''

<!-- The next quote is an unrelated teaching quotation. -->

> UNRELATED_QUOTE_ANCHOR This independent quotation must remain outside the figure caption.

<div style="break-before:page;height:224mm"></div>

## LIST_HEADING_ANCHOR

- **授课时间段**：00:11:14 — 00:12:12

LIST_BODY_ANCHOR '''+('The explanation must follow its heading and its teaching time on the same page. '*5)+'''

<div style="break-before:page;height:224mm"></div>

## PARAGRAPH_HEADING_ANCHOR

**授课时间段**：00:15:07 — 00:17:19

PARAGRAPH_BODY_ANCHOR '''+('The explanation must follow its heading and its teaching time on the same page. '*5)
            pdf,export=export_pdf(markdown,folder)
            self.assertEqual(export['render']['captionBlockquotes'],1)
            self.assertEqual(export['render']['standaloneBlockquotes'],1)
            self.assertEqual(export['render']['timeMetadataLists'],1)
            self.assertEqual(export['render']['timeMetadataParagraphs'],1)
            with fitz.open(pdf) as document:
                texts=[page.get_text() for page in document]
                def page_for(anchor):
                    matches=[index+1 for index,text in enumerate(texts) if anchor in text]
                    self.assertEqual(len(matches),1,anchor)
                    return matches[0]
                image_pages=[index+1 for index,page in enumerate(document) if page.get_images()]
                caption_page=page_for('FIGURE_CAPTION_ANCHOR')
                self.assertEqual(image_pages,[caption_page])
                self.assertEqual(page_for('LIST_HEADING_ANCHOR'),page_for('LIST_BODY_ANCHOR'))
                self.assertEqual(page_for('PARAGRAPH_HEADING_ANCHOR'),page_for('PARAGRAPH_BODY_ANCHOR'))
                self.assertIn('00:11:14',texts[page_for('LIST_BODY_ANCHOR')-1])
                self.assertIn('00:15:07',texts[page_for('PARAGRAPH_BODY_ANCHOR')-1])
                page_for('UNRELATED_QUOTE_ANCHOR')
                evidence=os.getenv('QWEN_ASR_LAYOUT_EVIDENCE_DIR')
                if evidence:
                    target=Path(evidence);target.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(pdf,target/'synthetic-layout.pdf')
                    numbers=sorted({caption_page,page_for('LIST_BODY_ANCHOR'),page_for('PARAGRAPH_BODY_ANCHOR')})
                    for number in numbers:
                        document[number-1].get_pixmap(matrix=fitz.Matrix(1,1),alpha=False).save(target/f'page-{number:03d}.png')
                    (target/'acceptance.json').write_text(json.dumps({'synthetic':True,'export':export,
                        'image_pages':image_pages,'caption_page':caption_page,'inspected_pages':numbers},indent=2))


if __name__=='__main__':unittest.main()
