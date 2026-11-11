"""Fast lesson note: one AGY turn writes the whole illustrated note.

Pipeline (target < 5 min, hard cap 10 min per lesson):
1. Non-AI: transcript blocks, lesson-scoped course files (material_scope),
   learning scope, 雨课堂 questions.
2. One AGY request with every input up front. The writer keeps the original
   teaching rules (TEACHING + saved system/custom prompts + learning-scope
   rules + 雨课堂 rules) and picks figures itself by writing
   ``![说明](figures/<source_id>_p<page>.png)`` for course pages it uses.
   A long note continues in the same conversation (2-3 output batches) via the
   existing writer fallback.
3. Non-AI: render the chosen pages, bind images, guarantee every forced
   雨课堂 question, direct Markdown → PDF, abstract from the note's opening.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from .ai import request_ai_text, provider_for_mode, AIRequestError
from .durable import atomic_bytes, atomic_json
from .note_content import transcript_blocks, clock
from .review_materials import collect, extract_image, bind_markdown_images
from .storage import local_now
from . import review_scope

VERSION = 'fast-one-turn-v7'
LOCATOR = re.compile(r'!\[[^\]]*\]\(\.?/?figures/([a-f0-9]{32})_p(\d+)\.png\)')

def abstract_from(markdown: str) -> str:
    """Library one-liner without another AI call: first real prose sentence."""
    for line in markdown.splitlines():
        text = re.sub(r'[#>*_`\[\]()!]|\(#source-\d+\)', '', line).strip()
        if len(text) >= 12 and not text.startswith(('课程：', '课次：', '新旧内容判断', '图')):
            text = re.split(r'(?<=[。！？])', text)[0]
            return text[:98]
    return ''


def _pages(package, *, complete=False):
    out = []
    pages = ([] if complete else package.get('relevant_pages')) or [
        # Without a transcript nothing is ranked: offer the lesson's own course
        # pages so the writer can still pick figures.
        {'source_id': s['source_id'], **p} for s in package.get('sources', []) for p in s.get('pages', [])]
    for page in pages:
        if (page.get('yuketang') or {}).get('missing_slide'):
            continue
        text = re.sub(r'\[\[AI_TRANSCRIPT_PAGE:\d+\]\]|SOURCE_PDF_PAGE: \d+', '', str(page.get('text') or '')).strip()
        out.append({'source_id': page['source_id'], 'page': int(page['page']), 'text': text,
                    **({'page_kind':'exercise', 'answer_authority':'personal_selection_is_not_an_answer'}
                       if (page.get('yuketang') or {}).get('student_selection') else {})})
    return out


def generation_identity(record, blocks, questions, selection, corpus, config):
    """A replaced course file or changed writer instructions cannot reuse a draft."""
    from . import note_prompts
    from .material_scope import VERSION as scope_version
    from .yuketang.pdf_layers import VERSION as image_policy
    materials = sorted([{'id':s['id'],'name':s.get('name'),'sha256':s['sha256'],
                         'conversion':{k:(s.get('conversion') or {}).get(k) for k in ('status','source_sha256','sha256')}}
                        for s in corpus if s.get('status') not in {'deleted','superseded','excluded'} and not s.get('superseded_by')],
                       key=lambda s:s['id'])
    values = [VERSION, note_prompts.fingerprint(), scope_version, image_policy,
              record.id, blocks, questions, selection, materials,
              config.get('_note_summary_system_prompt'), config.get('_note_summary_custom_prompt'),
              config.get('_note_material_coverage', 'lesson')]
    if config.get('_note_generation_id'):
        values.append({'generation_id': config['_note_generation_id']})
    return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()[:24]


WRITER_TIMEOUT = 1800  # long lessons (e.g. 光电子学) need >10min AGY turns


def _writer_config(config):
    """Bound one writer turn: a stalled AGY stream fails fast as a retriable
    read timeout instead of holding the lesson for the 30-minute default."""
    config = json.loads(json.dumps(config, default=str))
    provider = (config.get('ai') or {}).get('summary_provider')
    if isinstance(provider, dict) and provider.get('transport') == 'agy_cli':
        current = int(provider.get('cli_timeout_seconds') or 1800)
        provider['cli_timeout_seconds'] = WRITER_TIMEOUT if current == 0 else min(current, WRITER_TIMEOUT)
    return config


class ProposalKept(ValueError):
    """The edited body was kept; the new note is saved as a proposal (not a failure)."""


def _fallback_payload(payload):
    """Build an output-ownership ledger from the already selected inputs."""
    source = payload.get("transcript") or payload.get("source_pages") or []
    knowledge = [{"id": f"fast-{i}", "topic": str(block.get("text") or "课件内容")[:100],
                  "sources": [str(block.get("source") or block.get("source_id") or f"page-{i}")]}
                 for i, block in enumerate(source, 1)]
    if not knowledge:
        raise ValueError("没有可继续分批写作的课堂来源")
    return {**payload, "coverage": {"knowledge": knowledge, "figures": []}, "images": []}


async def generate_fast(record, config):
    from .review_notes import TEACHING, note_runtime_config, stage, body_hash, timestamp_references, disabled_review, pdf_failed_review
    from .batch_ai import _yuketang_questions
    from .note_pdf import export_pdf
    from .chapters import update_automatic_chapters
    started = time.monotonic()
    from . import note_prompts
    blocks = note_prompts.speech_blocks(transcript_blocks(record))
    initial = body_hash(record.metadata.get('ai_note_summary'))
    config = note_runtime_config(config)
    source_blocks = [{'source': 'S' + str(i), 'start': b['start'], 'end': b['end'], 'text': b['text']} for i, b in enumerate(blocks, 1)]
    questions = await _yuketang_questions(record, source_blocks)
    if not blocks:
        # Without speech there is nothing to rank optional questions against;
        # keep this lesson's own exercises so the note stays on topic.
        questions = [q for q in questions if q.get('必含')]
    selection = config.get('_review_material_ids', record.metadata.get('review_material_ids'))
    from .course_files import CourseFiles
    from .storage import app_settings
    corpus = await asyncio.to_thread(CourseFiles(app_settings).list, record.metadata.get('category'))
    identity = generation_identity(record, blocks, questions, selection, corpus, config)
    relative = f'.review-notes/{identity}'
    folder = record.directory / relative
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    task_path = folder / 'task.json'
    task = json.loads(task_path.read_text()) if task_path.exists() else {'id': identity, 'version': VERSION, 'created_at': local_now(), 'base_body_sha256': initial}
    timings = task.setdefault('timings', {})
    existing = record.metadata.get('ai_note_summary') or {}
    if (task.get('status') == 'completed' and existing.get('review_package') == relative
            and (folder/'note.md').is_file() and body_hash(existing) == hashlib.sha256((folder/'note.md').read_bytes()).hexdigest()):
        update_automatic_chapters(record)
        return existing

    def mark(name):
        timings[name] = round(time.monotonic() - started, 1)
        atomic_json(task_path, task)

    stage(record, task, folder, '匹配课件', status='running')
    from .async_tasks import await_owned_task
    package = await await_owned_task(asyncio.create_task(asyncio.to_thread(collect, record, folder, selection)))
    # Bind the lesson's chapters before PDF provenance is built. The default
    # fast pipeline must keep the same automatic metadata as the full pipeline.
    update_automatic_chapters(record, package)
    if package.get('material_selection'):
        task['material_selection'] = package['material_selection']
    if not blocks and not package.get('sources'):
        raise ValueError('录音没有可用转写，也没有选中课件，无法生成笔记')
    package['_has_transcript'] = bool(blocks)
    scope = await review_scope.prepare(package, folder, provider_for_mode(config.get('ai', {}), 'summary'))
    mark('materials')

    draft_path = folder / 'note.md'
    # The writer's raw output is kept apart from the processed note.md: a rerun
    # must restart from figure locators, not from already-bound asset links.
    raw_path = folder / 'writer.md'
    if raw_path.exists():
        markdown = raw_path.read_text()
    else:
        stage(record, task, folder, '编写笔记')
        from . import note_prompts
        from .settings import DEFAULT_NOTE_SUMMARY_PROMPT
        found = note_prompts.features(package, scope, questions)
        task['features'] = found
        saved = str(config.get('_note_summary_system_prompt') or '')
        custom = '\n'.join(x for x in (saved if saved.strip() != DEFAULT_NOTE_SUMMARY_PROMPT.strip() else '',
                                        str(config.get('_note_summary_custom_prompt') or '')) if x.strip())
        instructions = note_prompts.compose(found, custom)
        complete_materials = config.get('_note_material_coverage') == 'selected_files'
        if complete_materials:
            instructions += ('\n本次用户明确要求完整覆盖所选课件，录音存在缺失。本次范围是所选课件的全部页面与整个章节；'
                             '取消“只写本节录音讲到的内容、未讲到的后续不展开”的范围限制，仍按掌握/理解/了解控制深度。'
                             '完整讲解所有课件小节并逐题回答课件末尾习题；以“课件补充·原课件第X页”标明录音缺失部分，'
                             '只在真实转写支持时引用课堂时间和老师强调，不为课件补充编造时间或教师原话。')
        payload = {'transcript': source_blocks, 'source_pages': _pages(package, complete=complete_materials),
                   'course': package.get('category'), 'lesson_title': (package.get('material_selection') or {}).get('lesson_title', '')}
        task['material_coverage'] = 'selected_files' if complete_materials else 'lesson'
        task['material_page_manifest'] = [{'source_id': p['source_id'], 'page': p['page']} for p in payload['source_pages']]
        atomic_json(task_path, task)
        if found['learning_requirements']:
            payload['learning_requirements'] = note_prompts.requirement_items(scope)
        if questions:
            # Full questions remain available to the deterministic appendix;
            # only the bounded copy is sent to the model.
            from .yuketang.retrieval import fit as fit_yuketang
            payload['yuketang_questions'] = fit_yuketang(questions)
        try:
            if (folder / 'markdown-batches' / 'plan.json').is_file():
                raise AIRequestError('继续已保存的分批笔记', kind='output_truncated')
            try:
                markdown = await request_ai_text(instructions=instructions, payload=payload, config=_writer_config(config), mode='summary', max_output_tokens=32768)
            except AIRequestError as exc:
                if exc.kind not in {'network_read_timeout', 'network_transport'}:
                    raise
                markdown = await request_ai_text(instructions=instructions, payload=payload, config=_writer_config(config), mode='summary', max_output_tokens=32768)
        except AIRequestError as exc:
            if exc.kind != 'output_truncated':
                raise
            from .note_markdown_batches import write_batches
            markdown, _ = await write_batches(instructions, _fallback_payload(payload), config, folder)
        markdown = re.sub(r'^```(?:markdown)?\s*|\s*```\s*$', '', markdown.strip())
        atomic_bytes(raw_path, markdown.encode())
    mark('writing')

    # Figures the writer chose: render those original pages, drop bad locators.
    images, seen = [], set()
    valid = {(p['source_id'], int(p['page'])) for p in package.get('relevant_pages', [])} | {
        (s['source_id'], n) for s in package['sources'] for n in range(1, len(s['pages']) + 1)}
    for sid, page in LOCATOR.findall(markdown):
        key = (sid, int(page))
        if key in seen or key not in valid:
            continue
        seen.add(key)
        try:
            images.append(await asyncio.to_thread(extract_image, folder, package, {'source_id': sid, 'page': int(page), 'bbox': [0, 0, 1, 1]}))
        except ValueError:
            pass
    known = {(i['source_id'], i['page']) for i in images}
    markdown = LOCATOR.sub(lambda m: m[0] if (m[1], int(m[2])) in known else '', markdown)
    # The writer sometimes echoes image paths it saw elsewhere; keep only rendered ones.
    rendered = {i['path'] for i in images}
    markdown = re.sub(r'!\[[^\]]*\]\((?:\./)?([^)\s]+)(?:\s+"[^"]*")?\)\n?',
                      lambda m: m[0] if m[1] in rendered or m[1].startswith('figures/') else '', markdown)
    markdown = bind_markdown_images(markdown, folder, images)
    atomic_json(folder / 'images.json', images)

    # Transcript citations are resolved once, by timestamp_references below.
    from . import note_prompts
    markdown, cleaning = note_prompts.clean(markdown)
    markdown = note_prompts.readable_ranges(markdown if blocks else note_prompts.material_only(markdown))
    task['cleaning'] = cleaning
    markdown = re.sub(r'^# [^\n]*\n?', '', markdown, count=1, flags=re.M) if markdown.lstrip().startswith('# ') else markdown
    markdown = (f'# {record.metadata.get("title", "课堂复习笔记")}\n\n课程：{package["category"]}　课次：{note_prompts.human_date(package["lesson_date"])}\n\n'
                + markdown.lstrip())
    if questions:
        from .yuketang.retrieval import ensure_section, normalize_section, resolve_markers
        markdown = resolve_markers(normalize_section(ensure_section(markdown, questions)))
        from .yuketang.note_images import attach
        markdown, task['yuketang_images'] = attach(markdown, questions, folder, str(record.metadata.get('category') or ''))
    markdown = note_prompts.bold_cjk_safe(markdown)
    markdown = timestamp_references(markdown, blocks)
    atomic_bytes(draft_path, markdown.encode())

    stage(record, task, folder, '生成 PDF')
    provenance = {'layout_engine': 'chromium-markdown-katex-v1', 'pipeline': VERSION,
                  'course_files': {s['source_id']: s['sha256'] for s in package['sources']},
                  **({'learning_scope': {'enabled': True, 'sha256': review_scope.scope_hash(scope), 'version': review_scope.VERSION}} if scope.get('enabled') else {})}
    from .note_pdf import chapter_provenance
    provenance = chapter_provenance(provenance, record.metadata)
    provenance['material_coverage'] = config.get('_note_material_coverage', 'lesson')
    try:
        pdf, export = await asyncio.to_thread(export_pdf, markdown, folder, provenance)
        report = disabled_review(pdf, export, folder)
    except Exception as exc:  # the Markdown note is still published
        pdf = export = None
        report = pdf_failed_review(exc)
    mark('pdf')

    note = {'summary': markdown, 'abstract': abstract_from(markdown), 'source': 'post_recording', 'created_at': local_now(), 'updated_at': local_now(),
            'review_package': relative, 'material_version': provenance, 'pdf_sha256': (export or {}).get('pdf_sha256'),
            'review_status': report['status'], 'review_enabled': False, 'review_policy': 'disabled_by_settings',
            'source_map': {str(i): {'start': b['start'], 'end': b['end'], 'segment_ids': b.get('segment_ids', [])} for i, b in enumerate(blocks, 1)},
            'image_count': len(images), 'pipeline': VERSION}
    if report.get('policy') == 'pdf_failed':
        note.update(pdf_status='failed', pdf_error=report.get('error', ''))
    atomic_json(folder / 'proposed-note.json', note)
    current = json.loads(record.meta_path.read_text())
    if body_hash(current.get('ai_note_summary')) != task['base_body_sha256'] or (current.get('ai_note_summary') or {}).get('source') in {'edited', 'ideashell_pull', 'restored'}:
        record.metadata.update(current)
        record.metadata['ai_note_proposal'] = {'task_id': identity, 'review_status': report['status'], 'pdf_sha256': note['pdf_sha256']}
        record.save_metadata()
        raise ProposalKept('生成期间或此前正文被人工修改；新版已保留为候选，未覆盖人工编辑')
    record.metadata.update(current)
    record.metadata['ai_note_summary'] = note
    record.save_all()
    mark('total')
    stage(record, task, folder, '完成 · 未审核', status='completed')
    record.metadata.pop('ai_note_summary_draft', None)
    record.save_metadata()
    return note
