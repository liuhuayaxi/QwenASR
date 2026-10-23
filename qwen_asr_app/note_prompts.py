"""Composable, lesson-adaptive prompt modules for the one-turn lesson note.

Every note is a knowledge-point note made for review and for extracting
knowledge points: clear headings per point, definitions, formulas, key
conclusions. Modules are selected from detected lesson features:

- learning_requirements: the course PPT states 学习要求/重点 for this chapter
  -> structure around them with their depth; otherwise derive the outline
  from the PPT section order and the lecture.
- yuketang: 雨课堂 exercises exist -> 考点 markers + one flat 课上习题 list.
- preview: flipped classroom / 课前预习 material (judged from page structure) -> merge it.
- course: category -> prompts/courses/<slug>.md module.
Prompt text lives in prompts/ so it can be edited without code changes.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'


def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding='utf-8').strip()


BASE = _read('base.md')
FIGURES = '\n' + _read('figures.md')
REQUIREMENTS = '\n' + _read('requirements.md')
OUTLINE = '\n' + _read('outline.md')
PREVIEW = '\n' + _read('preview.md')
MATERIAL_ONLY = '\n' + _read('material_only.md')


def fingerprint() -> str:
    """Changes whenever any prompt file is edited, so a lesson is rewritten
    with the new prompts instead of reusing a cached draft."""
    import hashlib
    digest = hashlib.sha256()
    for path in sorted(PROMPTS_DIR.rglob('*.md')):
        digest.update(path.relative_to(PROMPTS_DIR).as_posix().encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()[:16]


def _normal(name: str) -> str:
    return re.sub(r'[\s（）()·_\-]+', '', str(name or '')).lower()


def course_modules() -> dict[str, dict]:
    """prompts/courses/<slug>.md: front matter ``categories: a, b`` + body."""
    out = {}
    for path in sorted((PROMPTS_DIR / 'courses').glob('*.md')):
        text = path.read_text(encoding='utf-8')
        m = re.match(r'---\s*\ncategories:\s*(.+?)\n---\s*\n(.*)', text, re.S)
        if m:
            out[path.stem] = {'categories': [c.strip() for c in m[1].split(',') if c.strip()], 'text': m[2].strip()}
    return out


def course_for(category: str) -> str | None:
    """Match the recording's course category (exact, then normalized); never filenames."""
    modules = course_modules()
    for slug, module in modules.items():
        if category in module['categories']:
            return slug
    key = _normal(category)
    for slug, module in modules.items():
        if key and any(_normal(c) == key for c in module['categories']):
            return slug
    return None


def speech_blocks(blocks: list[dict]) -> list[dict]:
    """Drop ASR hotword echoes: when audio has no speech the recogniser can
    return the hotword list (or the hotword prompt itself) for every chunk."""
    def key(text):
        return re.sub(r'[\W_]+', '', str(text or ''))
    counts: dict[str, int] = {}
    for block in blocks:
        counts[key(block.get('text'))] = counts.get(key(block.get('text')), 0) + 1
    def echo(text):
        text = str(text or '')
        return '术语词库' in text or (counts[key(text)] >= 3 and len(re.findall(r'[；;、，,]', text)) >= 5)
    return [b for b in blocks if key(b.get('text')) and not echo(b.get('text'))]


PREVIEW_DECLARED = re.compile(r'翻转课堂|课前预习|预习思考题|预习题|导学|自学任务')
QUESTION_LINE = re.compile(r'[^\n]{6,}[？?]\s*$', re.M)


def is_preview_source(source: dict) -> bool:
    """Judge by the material's own structure, not its filename: a preview /
    flipped-classroom handout declares itself on its first pages or is mostly
    question prompts (思考题 pages)."""
    pages = [str(p.get('text') or '') for p in source.get('pages', [])]
    if not pages:
        return False
    if any(PREVIEW_DECLARED.search(t) for t in pages[:3]):
        return True
    asking = sum(1 for t in pages if len(QUESTION_LINE.findall(t)) >= 2 or re.search(r'思考题\s*\d*\s*[:：]', t))
    return asking / len(pages) >= 0.25


def features(package: dict, scope: dict, questions: list) -> dict:
    return {
        'learning_requirements': bool(scope.get('enabled') and scope.get('items')),
        'yuketang': bool(questions),
        'preview': any(is_preview_source(s) for s in package.get('sources', [])),
        'transcript': bool(package.get('_has_transcript', True)),
        'course': course_for(str(package.get('category') or '')),
    }


def compose(found: dict, custom_prompt: str = '') -> str:
    parts = [BASE, FIGURES, REQUIREMENTS if found['learning_requirements'] else OUTLINE]
    if found.get('course'):
        parts.append('\n' + course_modules()[found['course']]['text'])
    if found['preview']:
        parts.append(PREVIEW)
    if not found.get('transcript', True):
        parts.append(MATERIAL_ONLY)
    if found['yuketang']:
        from .yuketang.retrieval import instructions
        parts.append(instructions(write_section=True))
    if custom_prompt.strip():
        parts.append('\n用户补充要求（不得与上面的格式禁令冲突；其中的“知识点速览”由“本节要点”承担，“公式速览”与“公式与结论汇总”合并为一张表，不重复成节；不写“待核对”）：\n' + custom_prompt.strip())
    return '\n'.join(parts)


def requirement_items(scope: dict) -> list[dict]:
    return [{'level': i.get('level'), 'section': i.get('section', ''), 'text': i.get('text', '')}
            for i in scope.get('items', [])] if scope.get('enabled') else []


# ---- post-processing (program-side guarantees) ---------------------------------
_META = re.compile(r'^\s*>?\s*(?:新旧内容判断范围|资料依据说明|<strong>资料依据说明|本笔记(?:以|仅|依据|根据)|.*文件源ID|.*源ID[:：]).*$', re.M)
_ASCII_ART = re.compile(r'[┌┐└┘├┤┬┴┼─│═║╔╗╚╝▲▼◀▶]|(?:[|/\\_\-+*]\s*){6,}|^\s*[|+].*[|+]\s*$', re.M)


def is_ascii_diagram(code: str) -> bool:
    lines = [l for l in code.splitlines() if l.strip()]
    if not lines:
        return False
    def drawn_line(l):
        solid = re.sub(r'\s', '', l)
        marks = len(re.findall(r'[|/\\_\-+^<>]', l))
        return bool(_ASCII_ART.search(l)) or (marks >= 2 and marks * 2 >= len(solid))
    drawn = sum(1 for l in lines if drawn_line(l))
    return drawn >= max(2, len(lines) // 3)


def clean(markdown: str) -> tuple[str, dict]:
    report = {'ascii_blocks_removed': 0, 'meta_lines_removed': 0, 'comments_removed': 0}
    def fence(match):
        body = match.group(2)
        if is_ascii_diagram(body):
            report['ascii_blocks_removed'] += 1
            return ''
        return match.group(0)
    markdown = re.sub(r'^```([^\n]*)\n(.*?)^```[ \t]*$\n?', fence, markdown, flags=re.M | re.S)
    markdown, n = re.subn(r'<!--(?!\s*yuketang:).*?-->\n?', '', markdown, flags=re.S)
    report['comments_removed'] = n
    markdown, n = _META.subn('', markdown)
    report['meta_lines_removed'] = n
    markdown = re.sub(r'[（(]\s*参考答案待核对\s*[）)]|【[^】]*待核对[^】]*】|（?待核对）?', '', markdown)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown, report


def bold_cjk_safe(markdown: str) -> str:
    """**…** that starts/ends with punctuation does not render next to CJK text
    (CommonMark flanking rules); use <strong> for those spans only."""
    def fix(match):
        inner = match.group(1)
        if re.search(r'[=√∑∫≈≤≥±×÷]', re.sub(r'\$[^$]*\$', '', inner)):
            return match.group(0)
        if re.match(r'^[\s（(“"《【\[]', inner) or re.search(r'[）)”"》】\]：:，。；、！？]$', inner):
            return f'<strong>{inner}</strong>'
        return match.group(0)
    return re.sub(r'\*\*([^*\n]{1,120}?)\*\*', fix, markdown)


def human_date(value: str) -> str:
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?', str(value or ''))
    if not m:
        return str(value or '')
    text = f'{int(m[1])}年{int(m[2])}月{int(m[3])}日'
    return text + (f' {m[4]}:{m[5]}' if m[4] else '')


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds // 60:02d}:{seconds % 60:02d}' if seconds < 3600 else f'{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def readable_ranges(markdown: str) -> str:
    """[373.4s - 663.4s] -> [06:13 - 11:03]."""
    return re.sub(r'\[(\d+(?:\.\d+)?)s\s*[-–]\s*(\d+(?:\.\d+)?)s\]',
                  lambda m: f'[{_clock(float(m[1]))} - {_clock(float(m[2]))}]', markdown)


def material_only(markdown: str) -> str:
    markdown = re.sub(r'\s*\[课堂 S[^\]]*\]\(#source-S?\d+\)', '', markdown)
    markdown = re.sub(r'^(#{1,4} .*?)\s*\[\d[\d:.s\s\-–]*\]\s*$', r'\1', markdown, flags=re.M)
    return re.sub(r'老师强调[：:]\s*', '', markdown)
