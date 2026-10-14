"""Only saved human content and accepted suggestions enter existing exports."""
from __future__ import annotations

import json

from ..note_content import clock


def saved_notes(record):
    if not getattr(record, 'directory', None):
        return []
    try:
        doc = json.loads((record.directory / 'classroom-notes.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    return [note for note in doc.get('notes', []) if str(note.get('content') or '').strip()]


def saved_notes_markdown(record):
    paragraphs = []
    for note in saved_notes(record):
        paragraphs.append(f"### {clock(note.get('start',0))}–{clock(note.get('end',0))}\n\n{note['content']}")
        used = {claim.get('citation_id') for claim in (note.get('accepted_claims') or [])}
        evidence = [e for e in note.get('evidence',[]) if not used or e.get('citation_id') in used]
        if evidence:
            refs = []
            for source in evidence:
                label = source.get('title') or ('本节课堂 ' + source.get('time',''))
                page = f" · 第 {source['page']} 页" if source.get('page') else ''
                target = source.get('open_url')
                location = f'[{label}{page}]({target})' if target else label+page
                refs.append(f"- [{source.get('citation_id','')}] {location}")
            paragraphs.append('依据：\n' + '\n'.join(refs))
    return '\n\n'.join(paragraphs)
