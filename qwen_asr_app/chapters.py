"""Conservative, read-only chapter suggestions from lesson-scoped materials.

This module does not render PDFs, convert documents, call AI, or scan other
lessons. A missing chapter heading remains unknown instead of being invented.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import os
import re
import zipfile
from xml.etree import ElementTree

VERSION = 'chapter-v1'
_SOURCES = {'manual', 'materials', 'lesson_title', 'unknown'}
_HEADING = re.compile(
    r'(?:第\s*(?P<cn>[0-9零〇一二两三四五六七八九十百]+)\s*章'
    r'|(?P<en>chapter|unit)\s*(?P<number>\d+|[IVXLC]+)(?=\b|[\u4e00-\u9fff]))', re.I)


def normalize_chapters(value: str | list) -> list[str]:
    """Validate editable chapter labels without silently losing user input."""
    if not isinstance(value, (str, list)):
        raise ValueError('章节必须是文字或文字列表')
    values = [value] if isinstance(value, str) else value
    result, seen = [], set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError('每个章节必须是文字')
        for part in re.split(r'[;；\r\n]+', value):
            label = re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f]', '', part)).strip()
            if not label:
                continue
            if len(label) > 120:
                raise ValueError('每个章节最多 120 个字符')
            key = label.casefold()
            if key not in seen:
                result.append(label)
                seen.add(key)
    if len(result) > 8:
        raise ValueError('一次最多关联 8 个章节')
    return result


def labels_from_metadata(metadata) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    for key in ('chapters', 'chapter'):
        value = metadata.get(key)
        if isinstance(value, (str, list)):
            try:
                labels = normalize_chapters(value)
            except ValueError:
                continue
            if labels:
                return labels
    return []


def _source_name(value) -> str:
    # Evidence is display data: never include an absolute local path.
    return str(value or '').replace('\\', '/').rsplit('/', 1)[-1][:240]


def chapter_fields(labels, source='unknown', evidence=None) -> dict:
    labels = normalize_chapters(labels)
    if source not in _SOURCES:
        raise ValueError('未知的章节来源')
    cleaned = []
    for row in (evidence or [])[:24]:
        if not isinstance(row, dict):
            continue
        page = row.get('page')
        cleaned.append({'source_name': _source_name(row.get('source_name')),
                        'page': page if type(page) is int and page > 0 else None,
                        'text': re.sub(r'\s+', ' ', str(row.get('text') or '')).strip()[:240]})
    return {'chapter': '；'.join(labels), 'chapters': labels,
            'chapter_info': {'source': source if labels or source == 'manual' else 'unknown',
                             'evidence': cleaned if labels else [], 'version': VERSION}}


def _number(value: str) -> int | None:
    if value.isdigit():
        number = int(value)
    elif re.fullmatch(r'[IVXLC]+', value, re.I):
        digits = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100}
        values = [digits[c] for c in value.upper()]
        number = sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))
    else:
        digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
        number, current = 0, 0
        for char in value:
            if char in digits:
                current = digits[char]
            elif char in '十百':
                number += (current or 1) * (10 if char == '十' else 100)
                current = 0
            else:
                return None
        number += current
    return number if 0 < number < 1000 else None


def _heading(line: str, *, filename=False):
    text = re.sub(r'\[\[AI_TRANSCRIPT_PAGE:\d+\]\]', '', str(line)).strip()
    text = re.sub(r'^[#*\s•●◆]+', '', text).strip()
    text = text.strip(' 《》「」【】')
    if filename:
        text = re.sub(r'\.(?:pdf|pptx?|docx?|txt|md)$', '', _source_name(text), flags=re.I)
        match = _HEADING.search(text)
    else:
        match = _HEADING.match(text)
    if not match:
        if re.fullmatch(r'(?:课程[-_ ]*)?绪论(?:[-_ ]*课件)?(?:[（(]\d+[）)])?', text):
            return ('intro', 0), '绪论', text
        if filename and '绪论' in text and not re.search(r'习题|复习|答案|目录', text):
            return ('intro', 0), '绪论', text
        return None
    number = _number(match.group('cn') or match.group('number'))
    if number is None:
        return None
    raw_tail = text[match.end():]
    # A slide headed "Chapter 2.1" identifies a section of chapter 2;
    # the section number must not become a fake title such as "Chapter 2 1".
    tail = re.sub(r'^\.\d+(?:\.\d+)*', '', raw_tail).strip(' \t-_—–:：.、/／')
    # Do not turn a generated note's explanatory prose or a question-bank
    # filename into the chapter title. The chapter number remains useful, but
    # the rest is not a reliable section name.
    if re.search(r'(?:本节|题目完整|纳入复习|答案解析|预习题|题目与答案|翻转课堂预习)', tail):
        tail = ''
    else:
        tail = re.split(r'[/／:：]', tail, maxsplit=1)[0].strip()
    tail = re.sub(r'[（(]\s*(?:20\d{2}[a-z]+|[a-z]+\d{4})\s*[）)]', '', tail, flags=re.I)
    tail = re.sub(r'\s*[》>]\s*[-–—]?\s*\d+(?:\s*/\s*\d+\s*页)?\s*[-–—]?\s*$', '', tail)
    tail = re.sub(r'\s*[（(]\s*\d+\s*[）)]\s*$', '', tail)
    # OCR and slide line wrapping often leaves a single space between Chinese
    # characters; collapse only those spaces, preserving spaces in English.
    tail = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', tail)
    # Exclude table-of-contents lines, prose references, and a second chapter
    # on the same line. These do not establish a page's current chapter.
    if _HEADING.search(tail) or re.search(r'(?:\.{2,}|…{2,}|·{3,})\s*\d*\s*$', tail):
        return None
    if re.search(r'[。！？!?；;]|^(?:介绍了|讨论了|已经|将会|中[的所])', tail):
        return None
    if re.match(r'(?:describes|discusses|introduces|reviews|will|was|is|has|covers)\b', tail, re.I):
        return None
    tail = re.sub(r'[（(]视频[^）)]*[）)]', '', tail)
    tail = re.sub(r'[-_ ]*(?:翻转课堂|预习思考题|预习题|课堂讨论|第二次课堂讨论|课件|幻灯片|PPT|slides|讲义).*$', '', tail, flags=re.I).strip()
    if filename:
        tail = re.sub(r'\s*[（(](?:AI|最终版|修订版)[）)]$', '', tail, flags=re.I).strip()
    if len(tail) > 70:
        return None
    is_unit = (match.group('en') or '').lower() == 'unit'
    prefix = f'Unit {number}' if is_unit else f'第{number}章' if match.group('cn') else f'Chapter {number}'
    return ('unit' if is_unit else 'chapter', number), prefix + (' ' + tail if tail else ''), text


def _page_headings(source: dict) -> list[dict]:
    result = []
    for page in source.get('pages') or []:
        if not isinstance(page, dict) or type(page.get('page')) is not int:
            continue
        text = str(page.get('text') or '')
        # AI-friendly caches retain the original text before the navigation
        # marker. Prefer that native prefix and exclude converter control tags
        # from titles when only the existing navigation layer is available.
        native = text.split('[[AI_TRANSCRIPT_PAGE:', 1)[0]
        if any(_heading(line) for line in native.splitlines()):
            text = native
        lines = [line.strip() for line in text.splitlines()
                 if line.strip() and not re.match(r'^\s*\[.*\]\s*$|^SOURCE_PDF_PAGE:|^The following is MinerU', line)]
        found = [(index, _heading(line)) for index, line in enumerate(lines[:12])]
        found = [(index, item) for index, item in found if item]
        # A book contents/overview page may name every chapter. Never assign
        # all of them to this lesson, including when it ranks in retrieval.
        if len({item[0] for _, item in found}) > 1:
            continue
        if found and re.search(r'目录|table\s+of\s+contents|^contents$|课程大纲', '\n'.join(lines[:3]), re.I):
            continue
        for index, (key, label, text) in found:
            prefix_only = re.fullmatch(r'第\d+章|(?:Chapter|Unit) \d+', label)
            original_heading = _HEADING.match(text.strip(' 《》「」【】'))
            original_tail = text[original_heading.end():].strip() if original_heading else 'not bare'
            bare_heading = not re.sub(r'^\.\d+(?:\.\d+)*', '', original_tail).strip(' .:：-—–')
            if prefix_only and bare_heading and index + 1 < len(lines):
                title = lines[index + 1].strip('#* \t')
                if (2 <= len(title) <= 70 and not _heading(title)
                        and not re.fullmatch(r'\d+|page\s*\d+', title, re.I)
                        and not re.search(r'email|邮箱|@', ' '.join(lines[index + 1:index + 4]), re.I)
                        and not re.search(r'^(?:本节|本章|本课程|本讲|根据|学习要求|复习|题目|\d+[.、]|https?://|\[)|[@。！？=]|教师|教授|学院|大学|主讲|copyright', title, re.I)):
                    label += ' ' + title
                    text += ' / ' + title
            if re.fullmatch(r'第\d+章|(?:Chapter|Unit) \d+', label):
                named = _heading(str(source.get('name') or ''), filename=True)
                if named and named[0] == key and not re.fullmatch(r'第\d+章|(?:Chapter|Unit) \d+', named[1]):
                    label = named[1]
                    text += ' / 文件名：' + _source_name(source.get('name'))
            result.append({'key': key, 'label': label, 'page': page['page'],
                           'source_name': _source_name(source.get('name')), 'text': text})
    return sorted(result, key=lambda item: item['page'])


def _lesson_title(package: dict, metadata: dict) -> str:
    selection = package.get('material_selection') or {}
    if not isinstance(selection, dict):
        selection = {}
    return str(selection.get('lesson_title') or metadata.get('lesson_title')
               or metadata.get('yuketang_lesson_title') or metadata.get('title') or '')


def _topic_terms(text) -> set[str]:
    text = _HEADING.sub(' ', str(text or '')).casefold()
    terms = set()
    for word in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]{3,}', text):
        if re.search(r'[\u4e00-\u9fff]', word):
            terms.update(word[i:i + size] for size in (2, 3) for i in range(len(word) - size + 1))
        else:
            terms.add(word)
    return terms - {'原理', '分类', '基本', '基础', '概念', '特性', '性能', '分析', '计算',
                    '结构', '机制', '关系', '讨论', '课堂', '光电', '电子', '半导', '导体',
                    '半导体', '技术', '系统', '的性', '器的', '与分', '与计', '课件', '讲义',
                    'chapter', 'unit', 'the', 'and', 'for', 'with', 'slides'}


def _uncertain_material_scope(package, metadata, labels) -> bool:
    """Do not turn a weak topic-only file selection into a certain chapter."""
    selection = package.get('material_selection') or {}
    if not isinstance(selection, dict):
        return False
    rows = [row for row in selection.get('files', []) if isinstance(row, dict) and row.get('chosen')]
    if len(rows) < 2 or any(row.get('mode') == 'manual' for row in rows):
        return False
    coverages = [row['coverage'] for row in rows if isinstance(row.get('coverage'), (int, float))]
    if not coverages or max(coverages) >= .5:
        return False
    title_terms = _topic_terms(metadata.get('title')) - _topic_terms(metadata.get('category'))
    if len(title_terms) < 3:
        return False
    return not any(len(title_terms & _topic_terms(label)) >= 2 for label in labels)


def infer_chapters(package, metadata=None) -> dict:
    """Infer only chapters supported by the selected lesson's source pages.

    Positive relevant-page scores map to the nearest preceding chapter heading.
    Weak adjacent-page scores are ignored, so a single boundary-page match does
    not automatically turn the whole lesson into a two-chapter lesson.
    """
    package = package if isinstance(package, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    selection = package.get('material_selection') or {}
    selection = selection if isinstance(selection, dict) else {}
    decisions = {row.get('source_id'): row for row in selection.get('files', []) if isinstance(row, dict)}
    sources = [row for row in package.get('sources', []) if isinstance(row, dict)]
    # Existing selection metadata can still supply filename evidence when the
    # cached package is unavailable; never include explicitly excluded files.
    if not sources:
        sources = [{'source_id': sid, 'name': row.get('name'), 'pages': []}
                   for sid, row in decisions.items() if row.get('chosen')]
    relevant = defaultdict(list)
    for row in package.get('relevant_pages') or []:
        if not isinstance(row, dict) or type(row.get('page')) is not int:
            continue
        score = row.get('score', 0)
        if isinstance(score, (int, float)) and score > 0:
            relevant[row.get('source_id')].append((row['page'], float(score)))
    lesson = _lesson_title(package, metadata)
    lesson_heading = _heading(lesson, filename=True)
    candidates = []
    source_scores = {sid: sum(score for _, score in pages) for sid, pages in relevant.items()}
    best_source_score = max(source_scores.values(), default=0)
    for source in sources:
        sid = source.get('source_id')
        decision = decisions.get(sid, {})
        if decision.get('chosen') is False:
            continue
        # Lesson packages may retain a few background/recap pages beside the
        # actual lesson. A weak source must not create a false extra chapter.
        if best_source_score and source_scores.get(sid, 0) < best_source_score * .25:
            continue
        headings = _page_headings(source)
        pages = relevant[sid]
        if not pages:
            pages = [(p, 1.0) for p in decision.get('pages', []) if type(p) is int and p > 0]
        if decision.get('mode') == 'no_signal_all' and not pages:
            continue
        supported = []
        if pages and headings:
            peak = max(score for _, score in pages)
            support = defaultdict(float)
            for page, score in pages:
                if score < peak * .35:
                    continue
                preceding = [item for item in headings if item['page'] <= page]
                if preceding:
                    support[preceding[-1]['key']] += score
            if support:
                cutoff = max(support.values()) * .25
                supported = [item for item in headings if support[item['key']] >= cutoff]
        if not supported and not pages:
            keys = {item['key'] for item in headings}
            if len(keys) == 1 and any(item['page'] <= 3 for item in headings):
                supported = headings
            elif lesson_heading:
                supported = [item for item in headings if item['key'] == lesson_heading[0]]
        if supported:
            candidates.extend(supported)
            continue
        filename = _heading(str(source.get('name') or ''), filename=True)
        if filename:
            candidates.append({'key': filename[0], 'label': filename[1], 'page': None,
                               'source_name': _source_name(source.get('name')), 'text': filename[2]})
    chosen = {}
    for candidate in candidates:
        key = candidate['key']
        # Keep order by material/page, but prefer the full title to a repeated
        # bare "第4章" running header for the same chapter.
        if key not in chosen or len(candidate['label']) > len(chosen[key]['label']):
            chosen[key] = candidate
    if chosen:
        items = list(chosen.values())[:8]
        if _uncertain_material_scope(package, metadata, [row['label'] for row in items]):
            return chapter_fields([], 'unknown')
        return chapter_fields([row['label'] for row in items], 'materials', items)
    if lesson_heading:
        return chapter_fields([lesson_heading[1]], 'lesson_title',
                              [{'source_name': '课次标题', 'page': None, 'text': lesson}])
    return chapter_fields([], 'unknown')


@lru_cache(maxsize=48)
def _native_source_pages(path: str, size: int, modified: int):
    # The key binds the in-memory cache to this exact file revision. Native
    # extraction is serial under native_pdf.PDF_LOCK and never renders pages.
    if Path(path).suffix.lower() == '.pdf':
        from .native_pdf import pdf_operation
        return pdf_operation('text', path=path, sort=True)
    with zipfile.ZipFile(path) as archive:
        slides = sorted((name for name in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', name)),
                        key=lambda name: int(re.search(r'(\d+)\.xml', name)[1]))
        return [{'page': index + 1, 'text': '\n'.join(node.text or '' for node in ElementTree.fromstring(archive.read(name)).iter()
                                                     if node.tag.endswith('}t'))}
                for index, name in enumerate(slides)]


def _course_originals(record):
    directory = getattr(record, 'directory', None)
    if not directory:
        return []
    directory = Path(directory).resolve()
    category = (getattr(record, 'metadata', {}) or {}).get('category')
    course = directory.parent.parent if directory.parent.name == '上课录音' else directory.parent
    if not category or course.name != category:
        return []
    excluded = {'上课录音', 'AI友好型', 'AI生成笔记'}
    originals = []
    # Scan just this course's originals. CourseFiles.list() is intentionally
    # avoided here: shared-folder discovery can write an index as a side effect.
    for root, folders, files in os.walk(course, followlinks=False):
        folders[:] = [name for name in folders if not name.startswith('.') and name not in excluded
                      and not (Path(root) / name).is_symlink()
                      and not (Path(root) / name / 'session.json').is_file()]
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() not in {'.pdf', '.pptx'} or '(AI)' in path.stem or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(course):
                continue
            try:
                stat = path.stat()
                if stat.st_size > 200 * 1024 * 1024:
                    continue
                pages = _native_source_pages(str(path), stat.st_size, stat.st_mtime_ns)
                originals.append({'source_id': hashlib.sha256(str(path.relative_to(course)).encode()).hexdigest()[:32],
                                  'name': name, 'pages': pages})
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ElementTree.ParseError):
                continue
    return originals


def _native_suggestion(record, metadata):
    title_terms = _topic_terms(metadata.get('title')) - _topic_terms(metadata.get('category'))
    if len(title_terms) < 2:
        return chapter_fields([], 'unknown')
    ranked = []
    for source in _course_originals(record):
        headings = _page_headings(source)
        filename = _heading(source['name'], filename=True)
        if filename and not any(row['key'] == filename[0] for row in headings):
            headings.append({'key': filename[0], 'label': filename[1], 'page': None,
                             'source_name': source['name'], 'text': source['name']})
        for heading in headings:
            heading_terms = _topic_terms(heading['label'])
            matches = title_terms & heading_terms
            score = len(matches) / max(1, min(len(title_terms), len(heading_terms)))
            if len(matches) >= 2 and score >= .25:
                ranked.append((score, source, heading))
    if not ranked:
        return chapter_fields([], 'unknown')
    best = max(score for score, _, _ in ranked)
    chosen, seen = [], set()
    for score, source, heading in sorted(ranked, key=lambda row: -row[0]):
        if score < best * .6 or heading['key'] in seen:
            continue
        chosen.append(heading); seen.add(heading['key'])
    return chapter_fields([row['label'] for row in chosen[:8]], 'materials', chosen[:8])


def suggestion_for_record(record) -> dict:
    """Reuse this note's package, then read current-course native originals.

    The fallback has no filesystem writes. It never visits other lessons,
    converted AI PDFs, external services, or any PDF rendering/conversion path.
    """
    metadata = getattr(record, 'metadata', {})
    metadata = metadata if isinstance(metadata, dict) else {}
    note = metadata.get('ai_note_summary') or {}
    note = note if isinstance(note, dict) else {}
    package = {}
    relative = note.get('review_package')
    if isinstance(relative, str) and relative and getattr(record, 'directory', None):
        root = Path(record.directory).resolve()
        try:
            path = (root / relative / 'materials.json').resolve()
            if path.is_relative_to(root) and path.is_file() and path.stat().st_size <= 32 * 1024 * 1024:
                value = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(value, dict):
                    package = value
        except (OSError, ValueError, TypeError):
            pass
    if not package:
        selection = metadata.get('material_selection') or note.get('material_selection')
        if isinstance(selection, dict):
            package = {'material_selection': selection}
    result = infer_chapters(package, metadata)
    if result['chapters']:
        return result
    return _native_suggestion(record, metadata)
