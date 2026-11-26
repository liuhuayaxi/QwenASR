"""AI review of section → courseware page links before they are shown.

``section_pages.build_links`` proposes a start page per leaf heading. Nothing
is shown until it is checked here: the reviewer model reads the heading, an
excerpt of the section body and the courseware text of the candidate page and
its neighbours (the same pages the viewer renders), and names the page where
that content starts. A link becomes ``verified`` only when the model is sure
and the page is one it was shown; a different page than proposed is accepted
only after a second, single-item confirmation. Everything else stays in
``review`` with the reason recorded, and is not shown.

Reviews are stored in the link document, so reruns only send headings that
were never reviewed (``merge_verified`` keeps earlier verdicts).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from . import section_pages as sp

logger = logging.getLogger(__name__)
REVIEWER = 'ai-review-v1'
BATCH = 10
BEFORE, AFTER = 2, 3
PAGE_CHARS, BODY_CHARS = 700, 900
INSTRUCTIONS = """你在核对课堂笔记的小节和课件页码，结果决定笔记标题旁是否显示“跳到课件第 N 页”的按钮，错误的按钮比没有按钮更糟。
每个条目给出：笔记小节标题（含上级标题路径）、该小节正文摘录、候选课件页及其前后几页的文字（[第N页] 为课件页码）。
请判断：这个小节讲的内容，在所给的课件页中从哪一页开始（即读者点按钮应该先看到的那一页）。
- 只有当所给某页确实在讲这个小节的主题（标题里的概念、公式、图、例题出现在该页），且它是这一内容的起始页时，才填 page 并令 sure=true。
- 内容在所给页中找不到、只是零散提及、横跨很多页无法确定起点、或是课堂补充/课后习题/总结而课件无对应页时，page 填 null，sure=false。
- 拿不准就 sure=false。
只输出一个 JSON 对象：{"items":[{"id":"条目id","page":整数或null,"sure":true或false,"why":"不超过25字的依据"}]}，每个条目恰好一项。"""
CONFIRM = """你在复核一个课件页码。给出笔记小节标题、正文摘录和一页课件文字。
问：读者从这个标题点“跳到课件”时，这一页是否就是该小节内容在课件中的起始页（该页确实在讲这个主题）？
只输出 JSON：{"ok":true或false,"why":"不超过25字"}。拿不准就填 false。"""


def _window(pages, start):
    n = len(pages)
    lo, hi = max(1, start - BEFORE), min(n, start + AFTER)
    return {p: sp.clean_page(pages[p - 1]).strip()[:PAGE_CHARS] for p in range(lo, hi + 1)}


def _items(doc, markdown, sources, force=False):
    heads = {h['index']: h for h in sp.headings(markdown)}
    texts = {s['source_id']: s['pages'] for s in sources}
    out = []
    for s in doc['sections']:
        if s['status'] == 'excluded' or not s.get('targets'): continue
        if s.get('review') and not force: continue
        t = s['targets'][0]
        pages = texts.get(t['source_id'])
        if not pages or not t.get('page_start'): continue
        h = heads.get(s['index'])
        body = sp.clean_note(h['body'] if h else '')[:BODY_CHARS]
        out.append({'section': s, 'pages': pages, 'window': _window(pages, t['page_start']),
                    'payload': {'id': s['section_id'], 'title_path': ' / '.join(sp.plain_heading(p) for p in s.get('path') or [s['heading']]),
                                'body_excerpt': body, 'candidate_page': t['page_start']}})
    return out


def _page_map(target):
    try: return json.loads((sp.PAGE_MAPS / f"{target.get('viewer_sha256')}.json").read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError): return None


def _apply(section, page, why, verdict):
    t = section['targets'][0]
    was = [t.get('page_start'), t.get('page_end')]
    if page is None:
        section['status'] = 'review'
        section['review'] = {'by': REVIEWER, 'verdict': verdict, 'why': why, 'auto_pages': was}
        return
    pm = _page_map(t)
    end = max(page, t.get('page_end') or page) if page >= (t.get('page_start') or page) else page
    viewer = sp.to_viewer(pm, page)
    if viewer is None:
        section['status'] = 'review'
        section['review'] = {'by': REVIEWER, 'verdict': 'no_viewer_page', 'why': why, 'auto_pages': was}
        return
    t.update(page_start=page, page_end=end, viewer_page_start=viewer, viewer_page_end=sp.to_viewer(pm, end) or viewer)
    section['status'] = 'verified'
    section['review'] = {'by': REVIEWER, 'verdict': verdict, 'why': why, 'auto_pages': was}


async def review(doc, markdown, sources, config, *, mode=None, concurrency=None, force=False, log=None):
    from .ai import request_ai_json, uses_agy_gemini, context_window_for, concurrency_for, lightweight_mode_for, AIRequestError
    from .context_budget import estimate_tokens
    mode = mode or lightweight_mode_for(config, fallback='summary')
    long_context = uses_agy_gemini(config, mode)
    if concurrency is None:
        concurrency = concurrency_for(config, mode) if long_context else 4
    items = _items(doc, markdown, sources, force)
    gate = asyncio.Semaphore(concurrency)
    stats = {'checked': 0, 'verified': 0, 'moved': 0, 'hidden': 0, 'errors': 0}

    async def confirm(item, page):
        payload = {'title_path': item['payload']['title_path'], 'body_excerpt': item['payload']['body_excerpt'],
                   'page': page, 'page_text': sp.clean_page(item['pages'][page - 1]).strip()[:PAGE_CHARS * 2]}
        async with gate:
            r = await request_ai_json(instructions=CONFIRM, payload=payload, config=config, mode=mode, max_output_tokens=400)
        return bool(r.get('ok')), str(r.get('why') or '')[:60]

    async def batch(group):
        payload = {'items': [{**i['payload'], 'pages': [{'page': p, 'text': x} for p, x in i['window'].items()]} for i in group]}
        if (long_context and len(group)>1 and estimate_tokens(INSTRUCTIONS)+estimate_tokens(json.dumps(payload,ensure_ascii=False))+32768+16000 > context_window_for(config,mode)):
            middle=len(group)//2
            await asyncio.gather(batch(group[:middle]),batch(group[middle:]))
            return
        try:
            async with gate:
                r = await request_ai_json(instructions=INSTRUCTIONS, payload=payload, config=config, mode=mode, max_output_tokens=200 + 120 * len(group))
        except Exception as exc:  # leave unreviewed; a rerun retries
            if long_context and isinstance(exc,AIRequestError) and exc.kind=='output_truncated' and len(group)>1:
                middle=len(group)//2
                await asyncio.gather(batch(group[:middle]),batch(group[middle:]))
                return
            logger.warning('section page review failed: %s', exc); stats['errors'] += len(group); return
        answers = {a.get('id'): a for a in (r.get('items') or []) if isinstance(a, dict)}
        async def apply_answer(item):
            s, a = item['section'], answers.get(item['payload']['id'])
            stats['checked'] += 1
            if not a: stats['errors'] += 1; return
            page, why = a.get('page'), str(a.get('why') or '')[:60]
            if not (a.get('sure') is True and isinstance(page, int) and page in item['window']):
                _apply(s, None, why, 'not_sure' if page is not None else 'no_page'); stats['hidden'] += 1; return
            if page != s['targets'][0]['page_start']:
                try: ok, why2 = await confirm(item, page)
                except Exception: stats['errors'] += 1; return
                if not ok: _apply(s, None, why + '；复核：' + why2, 'move_rejected'); stats['hidden'] += 1; return
                _apply(s, page, why + '；复核：' + why2, 'moved'); stats['moved'] += 1; stats['verified'] += 1; return
            _apply(s, page, why, 'checked'); stats['verified'] += 1
        await asyncio.gather(*(apply_answer(item) for item in group))
        if log: log(stats)

    groups = ([items] if items else []) if long_context else [items[k:k+BATCH] for k in range(0,len(items),BATCH)]
    await asyncio.gather(*(batch(group) for group in groups))
    return stats


async def refresh(directory: Path, session: dict, storage_root: Path, config, **kw):
    """Map, review and store links for one note (pipeline hook). Never touches the note."""
    if not sp.enabled(): return None
    markdown, sources, images, maps = sp.collect(directory, session, storage_root)
    doc = sp.document(session['id'], markdown, sp.build_links(markdown, sources, images, maps), sources, session.get('category') or '')
    doc = sp.merge_verified(doc, sp.load(session['id']))
    stats = await review(doc, markdown, sources, config, **kw)
    sp.save(doc)
    return stats
