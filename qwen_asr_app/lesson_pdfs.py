"""Keep saved lesson PDF versions with their recording, outside course materials."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil

from .durable import atomic_json
from .review_materials import safe_path


def lesson_pdf_path(record, ident):
    if not re.fullmatch(r'[a-f0-9]{32}', str(ident)):
        raise ValueError('笔记 PDF 编号无效')
    return safe_path(record.directory, f'.note-pdfs/{ident}.pdf')


def lesson_pdfs(record):
    values = []
    folder = safe_path(record.directory, '.note-pdfs')
    for meta in sorted(folder.glob('*.json')):
        try:
            if meta.is_symlink():
                continue
            entry = json.loads(meta.read_text())
            path = lesson_pdf_path(record, entry['id'])
            if meta.stem != entry['id'] or not path.is_file():
                continue
            values.append({**entry, 'download_url': f'/api/recordings/{record.id}/note-pdfs/{entry["id"]}'})
        except (ValueError, KeyError, OSError):
            continue
    return values


def archive_lesson_pdf(record, source, entry):
    source = Path(source)
    target = lesson_pdf_path(record, entry['id'])
    meta = target.with_suffix('.json')
    if target.exists() or meta.exists():
        raise ValueError('课堂笔记已有相同编号的 PDF，原文件已保留')
    target.parent.mkdir(parents=True, exist_ok=True)
    value = {key: entry.get(key) for key in ('id', 'title', 'filename', 'created_at')}
    value['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        shutil.move(str(source), str(target))
        atomic_json(meta, value)
    except BaseException:
        if target.exists():
            if source.exists():
                target.unlink()
            else:
                shutil.move(str(target), str(source))
        meta.unlink(missing_ok=True)
        raise
    return {**value, 'path': str(target), 'exists': True,
            'download_url': f'/api/recordings/{record.id}/note-pdfs/{entry["id"]}'}
