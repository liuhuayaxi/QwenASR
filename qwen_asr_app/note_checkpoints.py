"""Restore note progress from durable artifacts owned by one explicit task."""
from __future__ import annotations

import json
from pathlib import Path
import re


def checkpoint_draft(record):
    """Project disk receipts for display; this never authorizes source reuse."""
    draft = record.metadata.get('ai_note_summary_draft') or {}
    if draft.get('phase') in {'核对课件跳转', 'AI 取名'}:
        return draft
    saved = (record.metadata.get('summary_job') or {}).get('checkpoint') or {}
    ident = draft.get('review_task') or saved.get('review_task')
    if not re.fullmatch(r'[a-f0-9]{24}', str(ident or '')):
        return draft
    folder = record.directory / '.review-notes' / ident
    if folder.is_symlink() or not folder.resolve().is_relative_to(record.directory.resolve()):
        return draft
    try:
        task = json.loads((folder / 'task.json').read_text())
        if task.get('id') != ident:
            return draft
        phase = str(task.get('stage') or draft.get('phase') or saved.get('phase') or '')
        value = {**draft, 'review_task': ident, 'phase': phase,
                 'total_parts': task.get('total_parts', draft.get('total_parts', 0))}
        if phase.startswith('识别本节新内容'):
            parts = {}
            for path in folder.glob('coverage-part-*.json'):
                match = re.fullmatch(r'coverage-part-(\d+)\.json', path.name)
                if not match or path.is_symlink():
                    continue
                result = json.loads(path.read_text())
                if isinstance(result, dict) and isinstance(result.get('knowledge'), list):
                    parts[match[1]] = '已保存知识清单'
            value['parts'] = parts
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        return draft
