"""Content-addressed note snapshots; overwritten Markdown can always be restored."""
import copy
import difflib
import hashlib
import json
import re
from .durable import atomic_json


def version_id(note):
    return hashlib.sha256(str(note.get('summary') or '').encode()).hexdigest()


def capture(record, previous):
    if not isinstance(previous, dict) or not previous.get('summary'):
        return
    current = record.metadata.get('ai_note_summary') or {}
    if isinstance(current, dict) and current.get('summary') == previous.get('summary'):
        return
    path = record.directory / '.note-history' / (version_id(previous) + '.json')
    if not path.exists():
        atomic_json(path, copy.deepcopy(previous))


def versions(record):
    result = []
    for path in (record.directory / '.note-history').glob('*.json'):
        try:
            note = json.loads(path.read_text(encoding='utf-8'))
            result.append({'id': path.stem, 'updated_at': note.get('updated_at') or note.get('created_at', ''),
                           'source': note.get('source', ''), 'characters': len(note.get('summary', ''))})
        except (ValueError, OSError):
            continue
    return sorted(result, key=lambda item: (item['updated_at'], item['id']), reverse=True)


def read_version(record, ident):
    if not re.fullmatch(r'[a-f0-9]{64}', ident):
        raise ValueError('笔记版本编号无效')
    try:
        note = json.loads((record.directory / '.note-history' / (ident + '.json')).read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise ValueError('笔记版本不存在或无法读取') from None
    if version_id(note) != ident:
        raise ValueError('笔记版本校验失败')
    return note


def difference(before, after):
    # A bounded line diff keeps large notes from blocking the service.
    return '\n'.join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                        fromfile='历史版本', tofile='当前版本', lineterm=''))[:200_000]
