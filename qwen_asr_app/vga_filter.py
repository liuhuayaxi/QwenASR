"""Visual classification and section-local deduplication of emitted VGA pages."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile

import numpy as np
import pymupdf
from PIL import Image, ImageFilter

from .vga_slides import ExtractionCancelled, Slide, write_slides_pdf


SECTIONS = {'slide': '课件', 'document': '作业讲解'}


def visual_features(image):
    rgb = np.asarray(image.convert('RGB').resize((512, 384)), dtype=np.float32)
    gray = rgb.mean(axis=2)
    color = rgb.max(axis=2) - rgb.min(axis=2)
    center, saturation = gray[77:338, 118:481], color[77:338, 118:481]
    density = ((center < 170) & (saturation < 45)).mean(axis=1)
    return {
        'top_dark': float((gray[:32] < 100).mean()),
        'ribbon': float(((gray[:55] > 170) & (gray[:55] < 240) & (color[:55] < 25)).mean()),
        'top_edges': float((np.abs(np.diff(gray[:55], axis=1)) > 25).mean()),
        'icon_edges': float((np.abs(np.diff(gray[:300, :110], axis=1)) > 25).mean()),
        'rail': float((gray[40:350, :90] > 230).mean()),
        'gutter': float(((gray[40:350, 94:109] > 230) & (gray[40:350, 94:109] < 250)
                         & (color[40:350, 94:109] < 15)).mean()),
        'status': float(((gray[-18:] > 170) & (gray[-18:] < 240) & (color[-18:] < 25)).mean()),
        'taskbar_dark': float((gray[-18:] < 100).mean()),
        'side_color': float((np.concatenate((color[58:326, :10], color[58:326, -10:]), axis=1) > 45).mean()),
        'white': float(((center > 235) & (saturation < 20)).mean()),
        'text_rows': int(((density > .02) & (density < .40)).sum()),
        'color': float((saturation > 45).mean()),
    }


def classify_page(image):
    f = visual_features(image)
    content = f['white'] > .72 and f['text_rows'] > 20 and f['color'] < .04
    icon_grid = f['icon_edges'] > .025 or (f['icon_edges'] > .01 and f['taskbar_dark'] > .70)
    desktop = icon_grid and (f['white'] > .10 or f['color'] > .50)
    if f['side_color'] > .30 and desktop and not (content and f['ribbon'] > .25):
        return 'junk', '露出桌面壁纸或应用登录窗口'
    if f['top_dark'] > .50 and f['gutter'] > .65 and f['rail'] > .70:
        return 'junk', '深色标题栏和缩略图栏的窗口化放映'
    if .08 < f['top_dark'] < .95:
        if f['color'] > .09:
            return 'junk', '文件夹图标网格和窗口边框'
    if f['ribbon'] > .60 and f['white'] < .30 and f['text_rows'] < 20:
        return 'junk', '应用加载画面，缺少正文'
    ribbon = f['ribbon'] > .25 and f['top_edges'] > .025
    editor = ribbon and (f['rail'] > .55 or f['status'] > .30)
    if editor or (f['ribbon'] > .15 and content):
        return 'document', '编辑器工具栏和正文区域'
    if f['top_dark'] < .05 or f['top_dark'] > .95 or (f['top_edges'] < .025 and f['ribbon'] < .15):
        return 'slide', '全屏正文画面'
    return 'document', '无法确认是无关画面，保留正文'


def comparison(image):
    width = min(320, image.width)
    small = image.resize((width, max(1, round(image.height * width / image.width))))
    return np.asarray(small.convert('L').filter(ImageFilter.GaussianBlur(.6)), dtype=np.int16)


def comparison_metrics(a, b):
    if a.shape != b.shape:
        return 1.0, 1.0, 1.0

    def loss(old, new):
        background = float(np.median(old))
        ink = np.abs(old - background) > 20
        erased = ink & (np.abs(new - background) <= 20)
        return float(erased.sum() / max(1, ink.sum()))

    return float((np.abs(a - b) > 20).mean()), loss(a, b), loss(b, a)


@dataclass
class Page:
    slide: Slide
    kind: str
    position: int
    occurrences: list[dict]
    important: bool = False
    matched_snippets: list[dict] = field(default_factory=list)
    transcript_text: str = ''


@dataclass
class FilterResult:
    pages: list[Page]
    decisions: list[dict]
    enabled: bool
    warnings: list[str] = field(default_factory=list)
    recording_id: str | None = None

    def manifest(self):
        pages = []
        for index, page in enumerate(self.pages, 1):
            entry = page.slide.manifest()
            entry.update(index=index, source_index=page.slide.index, position_index=page.position + 1,
                         kind=page.kind, section=SECTIONS[page.kind], occurrences=page.occurrences,
                         important=page.important, matched_snippets=page.matched_snippets,
                         transcript_text=page.transcript_text)
            pages.append(entry)
        return {'pages': pages, 'dropped': [dict(d) for d in self.decisions if not d['kept']],
                'warnings': list(self.warnings), 'recording_id': self.recording_id}


def filter_slides(slides, *, enabled=True, keep_homework=True, cancel=None):
    with tempfile.TemporaryDirectory(prefix='qwen-vga-filter-') as raw:
        return _filter_slides(slides, enabled, keep_homework, cancel, Path(raw))


def _filter_slides(slides, enabled, keep_homework, cancel, cache):
    def check_cancel():
        if cancel and cancel():
            raise ExtractionCancelled('已取消课件过滤')

    pages, decisions, thumbnails = [], [], {}
    for position, slide in enumerate(slides):
        check_cancel()
        with Image.open(slide.image_path) as image:
            kind, detail = classify_page(image) if enabled else ('slide', '未启用过滤')
            if enabled:
                thumbnails[slide.index] = cache / f'{slide.index}.npy'
                np.save(thumbnails[slide.index], comparison(image).astype(np.uint8))
        occurrences = [dict(window) for window in slide.occurrences] or [slide.manifest()]
        pages.append(Page(slide, kind, position, occurrences))
        decisions.append({**slide.manifest(), 'source_index': slide.index, 'kind': kind,
                          'kept': True, 'detail': detail})
    if enabled:
        start = 0
        while start < len(pages):
            end = start + 1
            while end < len(pages) and pages[end].kind == pages[start].kind:
                end += 1
            if (pages[start].kind == 'slide' and end - start <= 2 and start > 0 and end < len(pages)
                    and pages[start - 1].kind == pages[end].kind == 'document'):
                for index in range(start, end):
                    pages[index].kind = decisions[index]['kind'] = 'document'
                    decisions[index]['detail'] = '前后均为作业讲解的短暂全屏画面'
            start = end
    kept, redirects = [], {}
    by_index = {d['source_index']: d for d in decisions}
    for page in pages:
        check_cancel()
        decision = by_index[page.slide.index]
        if page.kind == 'junk' or (enabled and page.kind == 'document' and not keep_homework):
            decision.update(kept=False, reason='junk' if page.kind == 'junk' else 'homework-disabled')
            continue
        match = None
        if enabled:
            current = np.load(thumbnails[page.slide.index]).astype(np.int16)
            for old in kept:
                check_cancel()
                if old.kind != page.kind:
                    continue
                reference = np.load(thumbnails[old.slide.index]).astype(np.int16)
                changed, forward, backward = comparison_metrics(reference, current)
                near = changed < .05 and forward < .05 and backward < .05
                subset = changed < .15 and min(forward, backward) < .03
                if near or subset:
                    match = old
                    old.occurrences.extend(page.occurrences)
                    if not near and forward < backward:
                        redirects[old.slide.index] = page.slide.index
                        old.slide = page.slide
                    else:
                        redirects[page.slide.index] = old.slide.index
                    break
        if match is None:
            kept.append(page)
    for source, target in redirects.items():
        while target in redirects:
            target = redirects[target]
        by_index[source].update(kept=False, reason='duplicate-of', duplicate_of=target, detail='重复或内容较少的画面')
    ordered = ([p for p in kept if p.kind == 'slide'] + [p for p in kept if p.kind == 'document'])
    return FilterResult(ordered, decisions, enabled)


def write_filtered_pdf(result, out_pdf, *, cancel=None):
    if not result.pages:
        raise ValueError('过滤后没有可保存的课件画面，请检查录屏内容或关闭过滤')
    if not result.enabled and not any(page.important for page in result.pages):
        write_slides_pdf([page.slide for page in result.pages], out_pdf)
        return result.manifest()
    destination = Path(out_pdf)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.vga-filter-', dir=destination.parent) as raw:
        with pymupdf.open() as pdf:
            toc, previous = [], None
            for index, item in enumerate(result.pages, 1):
                if cancel and cancel():
                    raise ExtractionCancelled('已取消课件保存')
                with Image.open(item.slide.image_path) as image:
                    width, height = image.size
                page = pdf.new_page(width=width, height=height)
                page.insert_image(page.rect, filename=str(item.slide.image_path))
                if item.kind != previous:
                    toc.append([1, SECTIONS[item.kind], index])
                    previous = item.kind
                if item.important:
                    toc.append([2, f'★ 第 {index} 页', index])
                    size = max(8, min(width, height) * .018)
                    box = pymupdf.Rect(5, 5, size * 3.5 + 5, size * 1.9 + 5)
                    page.draw_rect(box, color=None, fill=(1, 1, 1), fill_opacity=.9, overlay=True)
                    page.insert_text((box.x0 + size * .3, box.y0 + size * 1.3), '重点',
                                     fontname='china-s', fontsize=size, color=(.85, 0, 0), overlay=True)
                if item.kind == 'document':
                    size = max(8, min(width, height) * .018)
                    box = pymupdf.Rect(width - size * 5.5, 5, width - 5, size * 1.9 + 5)
                    page.draw_rect(box, color=None, fill=(1, 1, 1), fill_opacity=.85, overlay=True)
                    page.insert_text((box.x0 + size * .3, box.y0 + size * 1.3), '作业讲解',
                                     fontname='china-s', fontsize=size, color=(.25, .25, .25), overlay=True)
            pdf.set_toc(toc)
            staged = Path(raw) / 'filtered.pdf'
            pdf.save(staged, deflate=True)
        staged.replace(destination)
    return result.manifest()
