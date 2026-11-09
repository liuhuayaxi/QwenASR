"""Section → courseware page links ("一键跳转 PPT").

Links are stored beside the note, never inside it: the Markdown body and every
PDF stay untouched. Each heading is matched to pages of the lesson's original
courseware using, in priority order,

1. an explicit page reference in the heading (``课件 P69``),
2. figures embedded in the section (``images.json`` records their source page),
3. multi-evidence text matching (BM25 over the per-page transcript, smoothed so
   sections follow the teaching order), with a character n-gram similarity as
   the weakest, semantic-style signal.

Confidence decides display: ``high`` is shown automatically, ``medium`` goes to
a review list (shown only after verification), ``low`` is never shown.
Summary headings (本节要点、复习清单…) and 雨课堂 questions are never linked.

Page numbers: ``source_page`` is the 1-based physical page of the original PDF
(MinerU ``page_idx`` + 1, the ``[[AI_TRANSCRIPT_PAGE:n]]`` marker). The viewer
opens the AI-friendly PDF, whose physical page for each source page is read
from that PDF's own markers (``viewer_page``); no fixed offset is assumed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

from .durable import atomic_json
from .paths import PRIVATE_ROOT

SCHEMA_VERSION = 1
MAPPING_VERSION = 'section-pages-v1'
ROOT = PRIVATE_ROOT / 'section-page-links'
SETTINGS = ROOT / 'settings.json'
LINKS = ROOT / 'links'
PAGE_MAPS = ROOT / 'page-maps'
# Optional pre-rendered page images (sharingnotes docs/data/pages/<sha>/).
# Absent folders keep the viewer on the local PDF. This directory is never
# written by the request path.
PAGES = ROOT / 'pages'
PAGE_FILE = re.compile(r'(?:manifest\.json|t?[1-9]\d{0,3}\.(?:webp|png))\Z')

SUMMARY = re.compile(r'^(?:\d+(?:\.\d+)*\.?\s*)?(?:本节要点|本章学习要求|学习要求|学习目标|掌握|理解|了解|公式与结论汇总|核心公式(?:汇总表|速查表)?|关键结论|核心结论速查表|结论速查|复习清单|本节小结|小结|总结|课后作业)\s*$')
QUESTION = re.compile(r'雨课堂|随堂|自测|真题|考题练习|课堂练习题')
MARKER = re.compile(r'\[\[AI_TRANSCRIPT_PAGE:(\d+)\]\]')
EXPLICIT = re.compile(r'(?:课件|PPT|幻灯片|讲义)?\s*(?<![A-Za-z])[Pp]\.?\s*(\d{1,3})(?:\s*[-–~～]\s*[Pp]?\s*(\d{1,3}))?|第\s*(\d{1,3})\s*(?:[-–~～]\s*(\d{1,3})\s*)?页')
BACKOFF = .6
# Only checked links get a button; 'auto' (unreviewed high confidence) waits for review.
SHOWN = {'verified', 'manual'}
IMAGE = re.compile(r'!\[[^\]]*\]\((?:\./)?(assets/fig-[a-f0-9]{16}\.(?:png|jpe?g))\)')


def enabled():
    """On unless the owner explicitly turned the feature off.

    A missing settings file is the normal state of a new install, so new notes
    get buttons without a separate ``--enable`` step. ``--disable`` writes
    ``enabled: false`` and is the only opt-out.
    """
    try:
        value = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return True
    if not isinstance(value, dict) or 'enabled' not in value:
        return True
    return bool(value.get('enabled'))


def set_enabled(value: bool):
    ROOT.mkdir(parents=True, exist_ok=True)
    atomic_json(SETTINGS, {'enabled': bool(value)})


def page_image_manifest(sha_hex: str):
    """Published viewer manifest, or None when this PDF still uses PDF.js."""
    if not re.fullmatch(r'[a-f0-9]{64}', sha_hex or ''):
        return None
    try:
        value = json.loads((PAGES / sha_hex / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    pages = value.get('pages')
    sizes = value.get('sizes')
    if value.get('pdf_sha256') != sha_hex or isinstance(pages, bool) or not isinstance(pages, int) or pages <= 0:
        return None
    if not isinstance(sizes, list) or len(sizes) != pages:
        return None
    return value


def page_image_file(sha_hex: str, name: str):
    """One manifest or page image inside that PDF's folder, or None."""
    if page_image_manifest(sha_hex) is None or not PAGE_FILE.fullmatch(name or ''):
        return None
    folder = (PAGES / sha_hex).resolve()
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        return None
    return path


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def plain_heading(text: str) -> str:
    """Heading text as shown to the reader: no emphasis, links or stars."""
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    return re.sub(r'\s+', ' ', re.sub(r'[*`★☆]', '', text)).strip()


def headings(markdown: str):
    """ATX headings in document order, outside fenced code.

    ``index`` counts every heading, which equals the reader's heading order
    (sharingnotes ``section-(index+1)``; QwenASR top-level heading tokens).
    """
    result, fence, lines = [], None, markdown.replace('\r\n', '\n').split('\n')
    for number, line in enumerate(lines):
        stripped = line.lstrip(' ')
        marker = re.match(r'(`{3,}|~{3,})', stripped) if len(line) - len(stripped) <= 3 else None
        if marker:
            if fence is None: fence = marker[1][0]
            elif marker[1][0] == fence: fence = None
            continue
        if fence: continue
        match = re.match(r'^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$', line)
        if match:
            result.append({'index': len(result), 'level': len(match[1]), 'text': match[2].strip(), 'line': number})
    for position, item in enumerate(result):
        end = len(lines)
        for following in result[position + 1:]:
            end = following['line']; break
        item['body'] = '\n'.join(lines[item['line'] + 1:end])
        subtree_end = len(lines)
        for following in result[position + 1:]:
            if following['level'] <= item['level']: subtree_end = following['line']; break
        item['subtree_end'] = subtree_end
    stack = []
    for item in result:
        while stack and stack[-1]['level'] >= item['level']: stack.pop()
        item['path'] = [s['text'] for s in stack] + [item['text']]
        item['parent'] = stack[-1]['index'] if stack else None
        stack.append(item)
    return result


def section_id(item) -> str:
    fingerprint = sha(' / '.join(plain_heading(p) for p in item['path']))[:12]
    return f"h{item['index']}-{fingerprint}"


def exclusion(item, by_index):
    text = plain_heading(item['text'])
    text = re.sub(r'\s*[（(][^）)]*[）)]\s*$', '', text)  # “（掌握）” etc.
    if item['level'] == 1 and item['index'] == 0: return 'title'
    if SUMMARY.match(text): return 'summary'
    if QUESTION.search(text): return 'question'
    parent = by_index.get(item['parent']) if item['parent'] is not None else None
    if parent is not None:
        reason = exclusion(parent, by_index)
        if reason in {'summary', 'question'}: return reason
    return None


# ---------------------------------------------------------------- text model
BOILER = re.compile(r'^(?:\[\[(?:END_)?AI_TRANSCRIPT_PAGE:\d+\]\]|SOURCE_PDF_PAGE:.*|The following is MinerU.*|\[(?:TEXT|TABLE_HTML|FORMULA_LATEX|CAPTION|LIST)\]|\[VISUAL_ONLY[^\]]*\]|-\s*\d+\s*/\s*\d+\s*[页⻚]\s*-|《.*》)\s*$')
STOP = set('的了是在和与及或等中为对由其该这那一个我们可以进行通过如果所以因此就是需要以及'.replace('', ' ').split())


def clean_page(text: str) -> str:
    lines = [l for l in str(text or '').splitlines() if not BOILER.match(l.strip())]
    return re.sub(r'<[^>]+>', ' ', '\n'.join(lines))


def clean_note(text: str) -> str:
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)
    text = re.sub(r'\[([^\]]*)\]\(#source-[^)]*\)', ' ', text)
    text = re.sub(r'（讲授时间[^）]*）|\(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\)|\d{1,2}:\d{2}(?::\d{2})?', ' ', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    return re.sub(r'[*`#>|★☆]', ' ', text)


ROMAN = {'i': '1', 'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'l': '1', '|': '1', 'ⅰ': '1', 'ⅱ': '2', 'ⅲ': '3', 'ⅳ': '4', 'ⅴ': '5',
         '一': '1', '二': '2', '三': '3', '四': '4', '五': '5'}


def tokens(text: str):
    text = str(text).lower().replace('Ⅰ', 'ⅰ').replace('Ⅳ', 'ⅳ')
    out = []
    # “第I类 / 第|类(OCR) / 第ⅠV类 / 第4类” → one comparable token
    for match in re.finditer(r'第\s*([ivxl|ⅰⅱⅲⅳⅴ一二三四五1-5]{1,4})\s*类', text):
        raw = match[1].replace('ⅰv', 'iv').replace('ⅰ', 'i').replace('ⅳ', 'iv')
        value = ROMAN.get(raw, raw if raw.isdigit() else None)
        if value: out.extend([f'class{value}'] * 2)
    text = re.sub(r'第\s*[ivxl|ⅰⅱⅲⅳⅴ一二三四五1-5]{1,4}\s*类', ' 类 ', text)
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        chars = [c for c in run if c not in STOP]
        out.extend(a + b for a, b in zip(chars, chars[1:]))
    out.extend(w for w in re.findall(r'[a-z][a-z0-9]+|\d+(?:\.\d+)+(?:-\d+)?', text) if w not in {'the', 'and', 'mathrm', 'frac', 'text', 'left', 'right'})
    return out


def title_text(text: str) -> str:
    """The slide title: first short non-boilerplate lines of the page."""
    lines = [l.strip() for l in clean_page(text).splitlines() if l.strip()]
    return ' '.join(l for l in lines[:2] if len(l) <= 40)


NUMBERED_HEAD = re.compile(r'^(?:\[(?:TEXT|CAPTION|TITLE)\]\s*)?(?:§\s*)?(?:\d+(?:\.\d+)*[.、．]?|[一二三四五六七八九十]+[.、．]|[（(]\d+[)）])\s*\S')


def numbered_heads(text: str) -> list[str]:
    """Short numbered lines anywhere on the slide ("4. DMA传送的过程")."""
    return [l.strip() for l in clean_page(text).splitlines() if len(l.strip()) <= 40 and NUMBERED_HEAD.match(l.strip())]


class PageIndex:
    def __init__(self, pages):
        self.titles = [set(tokens(title_text(p))) for p in pages]
        self.heads = [[set(tokens(line)) for line in numbered_heads(p)] for p in pages]
        self.pages = [Counter(tokens(clean_page(p))) for p in pages]
        self.length = [sum(c.values()) or 1 for c in self.pages]
        self.avg = sum(self.length) / max(1, len(self.length))
        df = Counter(t for c in self.pages for t in c)
        n = len(self.pages)
        self.idf = {t: math.log(1 + (n - d + .5) / (d + .5)) for t, d in df.items()}

    def scores(self, query: Counter):
        k1, b, result = 1.4, .6, []
        for c, length in zip(self.pages, self.length):
            score = 0.0
            for term, weight in query.items():
                f = c.get(term)
                if f: score += weight * self.idf[term] * f * (k1 + 1) / (f + k1 * (1 - b + b * length / self.avg))
            result.append(score)
        return result

    def title_cover(self, heading_terms: set, page: int):
        if not heading_terms: return 0.0
        return len(heading_terms & self.titles[page]) / len(heading_terms)

    def head_cover(self, heading_terms: set, page: int):
        """Best single numbered line's share of the heading terms."""
        if not heading_terms or not self.heads[page]: return 0.0
        return max(len(heading_terms & line) for line in self.heads[page]) / len(heading_terms)

    def cover(self, terms, page: int):
        terms = set(terms)
        return len([t for t in terms if self.pages[page].get(t)]) / len(terms) if terms else 0.0

    def similarity(self, query: Counter, page: int):
        """Cosine similarity of IDF-weighted bigrams (semantic-style fallback)."""
        c = self.pages[page]
        dot = sum(w * c.get(t, 0) * self.idf.get(t, 0) ** 2 for t, w in query.items())
        a = math.sqrt(sum((w * self.idf.get(t, 0)) ** 2 for t, w in query.items()))
        b = math.sqrt(sum((f * self.idf.get(t, 0)) ** 2 for t, f in c.items()))
        return dot / (a * b) if a and b else 0.0


def section_query(item):
    query = Counter()
    for t in tokens(clean_note(plain_heading(item['text']))): query[t] += 3
    for t in tokens(clean_note(item['body'])): query[t] += 1
    return query


def explicit_pages(text: str, limit: int):
    pages = []
    for match in EXPLICIT.finditer(text):
        first = match[1] or match[3]
        last = match[2] or match[4] or first
        start, end = int(first), int(last)
        if 1 <= start <= end <= limit and end - start <= 15: pages.append((start, end))
    return pages


# ---------------------------------------------------------------- page maps
def viewer_page_map(pdf_path: Path, expected_sha: str | None = None):
    """sourcePage→viewerPage for an AI-friendly PDF, from its own page markers.

    Cached once per PDF content hash (one index per PDF, never a copy of it).
    """
    from .course_files import digest_file
    digest = digest_file(pdf_path)
    cache = PAGE_MAPS / f'{digest}.json'
    try:
        value = json.loads(cache.read_text(encoding='utf-8'))
        if value.get('sha256') == digest: return {**value, 'index_sha256': expected_sha, 'index_sha_stale': bool(expected_sha and expected_sha != digest)}
    except (OSError, ValueError):
        pass
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    mapping = {}
    for physical, page in enumerate(reader.pages, start=1):
        for marker in MARKER.findall(page.extract_text() or ''):
            mapping.setdefault(int(marker), physical)
    value = {'sha256': digest, 'index_sha256': expected_sha, 'index_sha_stale': bool(expected_sha and expected_sha != digest), 'pages': len(reader.pages), 'method': 'ai-transcript-markers' if mapping else 'identity',
             'source_to_viewer': {str(k): v for k, v in sorted(mapping.items())}}
    PAGE_MAPS.mkdir(parents=True, exist_ok=True)
    atomic_json(cache, value)
    return value


def to_viewer(page_map, page):
    if not page_map or page_map.get('method') == 'identity': return page
    return page_map['source_to_viewer'].get(str(page))


# ---------------------------------------------------------------- mapping
def _smooth(candidates, n_pages):
    """Pick one page per text-located section keeping the teaching order.

    ``candidates`` is a list of score arrays (normalised to 0..1). Going back
    in the deck is allowed (teachers revisit slides) but costs a penalty.
    """
    if not candidates: return []
    inf = float('-inf')
    best = [list(candidates[0])]
    back = [[-1] * n_pages]
    for scores in candidates[1:]:
        prev = best[-1]
        # max over previous pages with penalty for backward jumps
        row, arg = [], []
        for page in range(n_pages):
            value, choice = inf, -1
            for before in range(n_pages):
                penalty = (0.0 if page - before <= 6 else min(.5, .025 * (page - before - 6))) if page >= before else 0.35 + 0.04 * min(before - page, 10)
                v = prev[before] - penalty
                if v > value: value, choice = v, before
            row.append(value + scores[page]); arg.append(choice)
        best.append(row); back.append(arg)
    page = max(range(n_pages), key=lambda p: best[-1][p])
    path = [page]
    for step in range(len(candidates) - 1, 0, -1):
        page = back[step][page]; path.append(page)
    return path[::-1]


def build_links(markdown: str, sources: list[dict], images: list[dict], viewer_maps: dict):
    """Compute links for one note.

    ``sources``: [{source_id, name, sha256, pages: [text...]}] (original deck, page order)
    ``images``:  images.json entries (path, source_id, page)
    ``viewer_maps``: source_id -> viewer_page_map()
    """
    items = headings(markdown)
    by_index = {i['index']: i for i in items}
    image_pages = {i['path']: (i['source_id'], int(i['page'])) for i in images if i.get('path') and i.get('page')}
    indexes = {s['source_id']: PageIndex(s['pages']) for s in sources}
    single = sources[0] if len(sources) == 1 else None
    limit = max((len(s['pages']) for s in sources), default=0)
    results, located = {}, []
    for item in items:
        entry = {'section_id': section_id(item), 'index': item['index'], 'level': item['level'],
                 'heading': item['text'], 'path': item['path']}
        reason = exclusion(item, by_index)
        if reason:
            results[item['index']] = {**entry, 'excluded': reason}; continue
        evidence = []
        for start, end in explicit_pages(plain_heading(item['text']), limit):
            if single: evidence.append(('explicit', single['source_id'], start, end))
        for path in IMAGE.findall(item['body']):
            if path in image_pages:
                sid, page = image_pages[path]
                evidence.append(('figure', sid, page, page))
        heading_terms = set(tokens(clean_note(plain_heading(item['text']))))
        query = section_query(item)
        scores = {sid: index.scores(query) for sid, index in indexes.items()}
        results[item['index']] = {**entry, 'has_children': any(c['parent'] == item['index'] for c in items), 'evidence': evidence, 'query': query, 'heading_terms': heading_terms, 'scores': scores}
        located.append(item['index'])
    # 1. file choice: explicit/figure evidence wins, else the best text score
    for idx in located:
        r = results[idx]
        votes = Counter(e[1] for e in r['evidence'])
        r['source_id'] = votes.most_common(1)[0][0] if votes else (
            max(r['scores'], key=lambda sid: max(r['scores'][sid] or [0])) if r['scores'] else None)
    # 2. per-page evidence rows, smoothed along the teaching order
    for source in sources:
        sid, n, index = source['source_id'], len(source['pages']), indexes[source['source_id']]
        chain = [idx for idx in located if results[idx]['source_id'] == sid]
        rows = []
        for idx in chain:
            r = results[idx]
            raw = r['scores'][sid]
            top = max(raw) or 1.0
            row = [v / top + .5 * index.title_cover(r['heading_terms'], p) for p, v in enumerate(raw)]
            for kind, esid, start, end in r['evidence']:
                if esid == sid:
                    for p in range(start, end + 1): row[p - 1] += 2.0 if kind == 'explicit' else .6
            r['row'] = row
            rows.append(row)
        for idx, page in zip(chain, _smooth(rows, n)):
            results[idx]['smoothed'] = page + 1
    # 3. start page, range and confidence for each section
    previous_start = {}
    for idx in located:
        r = results[idx]
        sid = r['source_id']
        index, raw, row = indexes[sid], r['scores'][sid], r['row']
        chosen = r['smoothed']
        order = sorted(range(len(row)), key=lambda p: -row[p])
        best = order[0] + 1
        second = next((row[p] for p in order[1:] if abs(p + 1 - best) > 1), 0.0)
        margin = row[best - 1] / second if second else 9.0
        evidence = [e for e in r['evidence'] if e[1] == sid]
        explicit = [e for e in evidence if e[0] == 'explicit']
        figures = sorted(e[2] for e in evidence if e[0] == 'figure')
        if explicit:
            start, end, method = explicit[0][2], explicit[0][3], 'explicit'
        else:
            start = end = chosen
            # move back to where the topic starts: preceding pages that still
            # match (titles repeat across a topic's slides), never before the
            # previous section's start and at most 4 pages.
            floor = previous_start.get(sid, 0) + 1
            peak = max(raw[chosen - 1], 1e-9)
            own_title = index.title_cover(r['heading_terms'], chosen - 1)
            while start - 1 >= max(floor, chosen - 3) and (
                    raw[start - 2] >= BACKOFF * peak
                    or (index.title_cover(r['heading_terms'], start - 2) >= max(.5, own_title + .01) and raw[start - 2] >= .35 * peak)):
                start -= 1
            # a numbered divider line ("4. DMA传送的过程") on a page just before the
            # best match opens the topic even though that page scores low; parents
            # are left to their children (step 4)
            if not r['has_children']:
                need = max(.2, own_title + .1, index.head_cover(r['heading_terms'], chosen - 1) + .1)
                for q in range(start - 1, max(floor, chosen - 3) - 1, -1):
                    if index.head_cover(r['heading_terms'], q - 1) >= need and max(len(r['heading_terms'] & line) for line in index.heads[q - 1]) >= 2:
                        start = q; break
            p = end  # extend over following pages that keep matching (or are image-only)
            while p < len(raw) and p < chosen + 4 and (raw[p] >= .55 * peak or not index.pages[p]): p += 1
            end = p
            if figures: end = max(end, max(f for f in figures if f <= chosen + 6) if any(f <= chosen + 6 for f in figures) else end)
            method = 'text' + ('+figure' if figures else '') + ('+title' if index.title_cover(r['heading_terms'], chosen - 1) >= .5 else '')
        cover = index.cover(r['query'], chosen - 1)
        title = max(index.title_cover(r['heading_terms'], p - 1) for p in range(start, end + 1))
        figure_agrees = any(start - 1 <= f <= end + 1 for f in figures)
        agrees = abs(chosen - best) <= 1
        on_page_figure = any(start <= f <= end for f in figures)
        similarity = index.similarity(r['query'], chosen - 1)
        if explicit:
            confidence = 'high'
        elif agrees and margin >= 1.15 and (similarity >= .35 or ((title >= .5 or on_page_figure) and similarity >= .2)):
            confidence = 'high'
        elif (agrees or figure_agrees) and (similarity >= .2 or title >= .34):
            confidence = 'medium'
        else:
            confidence = 'low'
        r.update(start=start, end=end, method=method, confidence=confidence, signals={
            'margin': round(margin, 2), 'coverage': round(cover, 2), 'title': round(title, 2),
            'best_page': best, 'smoothed_page': chosen, 'figures': figures,
            'similarity': round(similarity, 3)})
        if confidence != 'low': previous_start[sid] = start
    # 4. parents span their located children
    for item in reversed(items):
        r = results[item['index']]
        if 'start' not in r: continue
        children = [results[c['index']] for c in items if c['parent'] == item['index']]
        children = [c for c in children if 'start' in c and c['confidence'] != 'low' and c['source_id'] == r['source_id']]
        if not children:
            if not any(results[c['index']].get('start') for c in items if c['parent'] == item['index']): continue
            if r['method'] != 'explicit': r['confidence'] = 'low'  # none of its parts is in this deck
            continue
        first = min(c['start'] for c in children)
        own = r['start']
        r['start'] = own if (first - 3 <= own <= first and r['confidence'] != 'low') else first
        r['end'] = min(max(max(c['end'] for c in children), r['start']), r['start'] + 25)
        strong = sum(1 for c in children if c['confidence'] == 'high')
        r['method'] += '+children'
        r['confidence'] = 'high' if strong * 2 >= len(children) and children[0]['confidence'] == 'high' else 'medium'
    # 5. output
    output, source_by_id = [], {s['source_id']: s for s in sources}
    for item in items:
        r = results[item['index']]
        entry = {k: r[k] for k in ('section_id', 'index', 'level', 'heading', 'path')}
        if 'excluded' in r:
            output.append({**entry, 'status': 'excluded', 'reason': r['excluded'], 'targets': []}); continue
        if 'start' not in r:
            output.append({**entry, 'status': 'unresolved', 'confidence': 'low', 'method': 'none', 'targets': []}); continue
        source, page_map = source_by_id[r['source_id']], viewer_maps.get(r['source_id'])
        target = {'source_id': r['source_id'], 'source_name': source['name'], 'source_sha256': source['sha256'],
                  'viewer_sha256': (page_map or {}).get('sha256'), 'page_start': r['start'], 'page_end': r['end'],
                  'viewer_page_start': to_viewer(page_map, r['start']), 'viewer_page_end': to_viewer(page_map, r['end'])}
        output.append({**entry, 'status': {'high': 'auto', 'medium': 'review', 'low': 'unresolved'}[r['confidence']],
                       'confidence': r['confidence'], 'method': r['method'], 'signals': r['signals'], 'targets': [target]})
    return output


def document(recording_id, markdown, sections, sources, category=''):
    return {'schema_version': SCHEMA_VERSION, 'mapping_version': MAPPING_VERSION, 'recording_id': recording_id, 'category': category,
            'note_body_sha': sha(markdown), 'resources': [
                {'source_id': s['source_id'], 'name': s['name'], 'source_sha256': s['sha256'],
                 'viewer_name': s.get('viewer_name'), 'viewer_sha256': s.get('viewer_sha256'),
                 'page_mapping': s.get('page_mapping_method')} for s in sources],
            'sections': sections}


def links_path(recording_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{6,80}', recording_id): raise ValueError('录音 ID 无效')
    return LINKS / f'{recording_id}.json'


def load(recording_id):
    try:
        return json.loads(links_path(recording_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def merge_verified(new, old):
    """Keep manual verdicts (verified / rejected / manual pages) across reruns."""
    if not old: return new
    previous = {s['section_id']: s for s in old.get('sections', [])}
    for section in new['sections']:
        before = previous.get(section['section_id'])
        if before and (before.get('status') in {'verified', 'rejected', 'manual'} or before.get('review')):
            section.update({k: before[k] for k in ('status', 'targets', 'confidence', 'method', 'review') if k in before})
    return new


def apply_review(doc, truth, reviewer='manual'):
    """Record checked verdicts. ``truth`` rows: [heading prefix, starts|None, [a, b], 'uncertain'?].

    Certain rows become ``verified`` (page fixed to an accepted start) or
    ``rejected`` (topic not in this deck); uncertain rows stay in review with
    the suggested page so a person can confirm them.
    """
    page_maps = {}
    for r in doc.get('resources', []):
        try: page_maps[r['source_id']] = json.loads((PAGE_MAPS / f"{r['viewer_sha256']}.json").read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError): pass
    for section in doc['sections']:
        if section['status'] == 'excluded': continue
        row = next((t for t in truth if section['heading'].startswith(t[0])), None) or next((t for t in truth if t[0] in section['heading']), None)
        if not row: continue
        uncertain = len(row) > 3
        if row[1] is None:
            section.update(status='rejected', review={'by': reviewer, 'verdict': 'not_in_deck'}); continue
        target = (section.get('targets') or [None])[0]
        if target is None:
            resource = doc['resources'][0]
            target = {'source_id': resource['source_id'], 'source_name': resource['name'], 'source_sha256': resource['source_sha256'],
                      'viewer_sha256': resource.get('viewer_sha256')}
        start = target.get('page_start') if target.get('page_start') in row[1] else row[1][0]
        end = max(start, row[2][1])
        page_map = page_maps.get(target['source_id'])
        was = tuple((section.get('review') or {}).get('auto_pages') or (target.get('page_start'), target.get('page_end')))
        target.update(page_start=start, page_end=end, viewer_page_start=to_viewer(page_map, start), viewer_page_end=to_viewer(page_map, end))
        section['targets'] = [target]
        section['review'] = {'by': reviewer, 'verdict': 'uncertain' if uncertain else 'checked', 'auto_pages': list(was)}
        section['status'] = 'review' if uncertain else 'verified'
    return doc


def save(doc):
    path = links_path(doc['recording_id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, doc)
    return path


def public_view(doc, markdown):
    """What readers get: only shown links, each checked against the live heading."""
    if not doc: return None
    current = {h['index']: h['text'] for h in headings(markdown)}
    sections = []
    for s in doc.get('sections', []):
        if s.get('status') not in SHOWN or not s.get('targets'): continue
        if current.get(s['index']) != s['heading']: continue  # heading changed since mapping
        sections.append({'index': s['index'], 'heading': s['heading'], 'section_id': s['section_id'],
                         'targets': [{k: t.get(k) for k in ('source_id', 'source_name', 'page_start', 'page_end',
                                     'viewer_page_start', 'viewer_page_end')} for t in s['targets']]})
    return {'mapping_version': doc.get('mapping_version'), 'stale': doc.get('note_body_sha') != sha(markdown),
            'sections': sections}


# ---------------------------------------------------------------- inputs
def ai_pdf_for(storage_root: Path, category: str, source_id: str, source_sha: str):
    """The AI-friendly PDF registered for an original courseware file."""
    folder = Path(storage_root) / category
    index = json.loads((folder / '.qwenasr-files' / 'index.json').read_text(encoding='utf-8'))
    item = index.get(source_id) or {}
    conversion = item.get('conversion') or {}
    relative = conversion.get('relative_path')
    if item.get('sha256') != source_sha: raise ValueError('课件原件已变化，需要重新建立页码映射')
    if conversion.get('status') != 'completed' or not relative: raise ValueError('该课件尚无 AI 友好版 PDF')
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file(): raise ValueError('AI 友好版 PDF 不存在')
    return path, conversion.get('output_sha256')


def collect(directory: Path, session: dict, storage_root: Path):
    note = session.get('ai_note_summary') or {}
    markdown = str(note.get('summary') or '')
    package = str(note.get('review_package') or '')
    if not markdown.strip(): raise ValueError('这篇笔记还没有正文')
    if not package: raise ValueError('这篇笔记没有课件资料包，无法定位页码')
    folder = (Path(directory) / package).resolve()
    if not folder.is_relative_to(Path(directory).resolve()): raise ValueError('资料包路径越界')
    materials = json.loads((folder / 'materials.json').read_text(encoding='utf-8'))
    try: images = json.loads((folder / 'images.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): images = []
    sources, maps = [], {}
    for s in materials.get('sources', []):
        if s.get('origin', 'original') != 'original' or not s.get('pages'): continue
        texts = [p.get('text', '') for p in sorted(s['pages'], key=lambda p: p['page'])]
        entry = {'source_id': s['source_id'], 'name': s['name'], 'sha256': s['sha256'], 'pages': texts}
        try:
            path, expected = ai_pdf_for(storage_root, session.get('category') or '', s['source_id'], s['sha256'])
            page_map = viewer_page_map(path, expected)
            maps[s['source_id']] = page_map
            entry.update(viewer_name=path.name, viewer_sha256=page_map['sha256'], page_mapping_method=page_map['method'])
        except (OSError, ValueError) as exc:
            entry.update(viewer_error=str(exc))
        sources.append(entry)
    return markdown, sources, images, maps


def generate(directory: Path, session: dict, storage_root: Path):
    markdown, sources, images, maps = collect(directory, session, storage_root)
    sections = build_links(markdown, sources, images, maps)
    return document(session['id'], markdown, sections, sources, session.get('category') or '')
