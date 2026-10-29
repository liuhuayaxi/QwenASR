"""Read the original slide underneath Yuketang's browser-print answer UI.

Only the known print layout is handled: a full slide bitmap, separate circular
A-H controls, and the 《title》 / page footer. Teacher content inside the bitmap
is retained byte for byte. Never infer an answer from a control's colour.
Imported originals are not modified; callers render a temporary clean page.
This module is imported only inside the short-lived native PDF worker.
"""
from __future__ import annotations

import re

VERSION = 'yuketang-slide-layer-v1'


def inspect_page(page):
    import pymupdf as fitz
    text = page.get_text()
    if not re.search(r'《[^》\n]+》', text) or not re.search(r'\d+\s*/\s*\d+\s*页', text):
        return {}
    images = page.get_image_info(xrefs=True)
    slides = [i for i in images if i['width'] >= 500 and i['height'] >= 300
              and fitz.Rect(i['bbox']).get_area() >= page.rect.get_area() * .12]
    spans = [s for b in page.get_text('dict')['blocks'] if b['type'] == 0
             for line in b['lines'] for s in line['spans'] if re.fullmatch('[A-H]', s['text'].strip())]
    controls = []
    centres = [(fitz.Rect(s['bbox']).tl + fitz.Rect(s['bbox']).br) / 2 for s in spans]
    for drawing in page.get_drawings():
        rect, fill = drawing['rect'], drawing.get('fill')
        if not fill or not (8 <= rect.width <= 65 and .85 <= rect.width / max(1, rect.height) <= 1.15):
            continue
        gray = max(fill) - min(fill) < .03 and .45 < fill[0] < .7
        green = .10 < fill[0] < .22 and .75 < fill[1] < .87 and .36 < fill[2] < .50
        if not (gray or green) or not any(rect.contains(point) for point in centres):
            continue
        controls.append(rect)
    if not slides and any(i['width'] <= 32 and i['height'] <= 32 for i in images):
        return {'missing_slide': True, **({'kind': 'exercise', 'student_selection': True} if controls else {})}
    if len(controls) < 2:
        return {}
    affected = [i for i in slides if any(fitz.Rect(i['bbox']).contains(r) for r in controls)]
    # A browser broken-image icon (28x32) plus controls means the slide was
    # absent at print time. Scaling that icon cannot recover the question.
    if not affected:
        return {'kind': 'exercise', 'missing_slide': True, 'student_selection': True}
    return {'kind': 'exercise', 'student_selection': True,
            'slides': [{'xref': i['xref'], 'bbox': list(i['bbox'])} for i in affected]}


def clean_page(doc, page, info):
    """Return a temporary PDF with exact base bitmaps above the personal UI."""
    import pymupdf as fitz
    if info.get('missing_slide'):
        raise ValueError('雨课堂原件此页缺少题目底图（打印时未加载）；请换用完整原件')
    clean = fitz.open()
    target = clean.new_page(width=page.rect.width, height=page.rect.height)
    target.show_pdf_page(target.rect, doc, page.number)
    for slide in info.get('slides', []):
        original = doc.extract_image(slide['xref'])
        target.insert_image(fitz.Rect(slide['bbox']), stream=original['image'], overlay=True)
    return clean, target
