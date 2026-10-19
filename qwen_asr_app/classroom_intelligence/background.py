"""Small, stat-invalidated job headers for idle recovery and task listings."""
from __future__ import annotations

from collections import OrderedDict
import copy
import json
from types import SimpleNamespace

from ..recording_catalog import file_stamp
from .store import read_notes_doc


class NoteJobIndex:
    def __init__(self, max_bytes=2 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.bytes = 0

    def read(self, record):
        path = record.directory / 'classroom-notes.json'
        stamp = file_stamp(path)
        cached = self.entries.get(path)
        if cached is not None and cached[0] == stamp:
            self.entries.move_to_end(path)
            return cached[1], copy.deepcopy(cached[2])
        doc = read_notes_doc(record) if stamp is not None else {}
        ident = str(doc.get('recording_id') or record.id)
        rows = []
        for note in doc.get('notes', []):
            if not isinstance(note, dict) or (not note.get('job') and note.get('status') != 'pending'):
                continue
            row = {key: note.get(key) for key in
                   ('id', 'status', 'locked', 'error', 'created_at', 'updated_at')}
            job = note.get('job') or {}
            row['job'] = {key: job.get(key) for key in ('status', 'retryable', 'next_retry_at')}
            rows.append(row)
        if rows and not ident:
            ident = str(json.loads((record.directory / 'session.json').read_text())['id'])
        size = len(json.dumps([ident, rows], ensure_ascii=False).encode('utf-8')) + 128
        previous = self.entries.pop(path, None)
        if previous is not None:
            self.bytes -= previous[3]
        while self.entries and (self.bytes + size > self.max_bytes or len(self.entries) >= 2048):
            self.bytes -= self.entries.popitem(last=False)[1][3]
        if size <= self.max_bytes:
            self.entries[path] = (stamp, ident, rows, size)
            self.bytes += size
        return ident, copy.deepcopy(rows)

    def records(self, store):
        directories = getattr(store, 'recording_directories', None)
        if callable(directories):
            seen = set()
            for directory in directories():
                seen.add(directory / 'classroom-notes.json')
                try:
                    ident, rows = self.read(SimpleNamespace(directory=directory, id=''))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if rows and ident:
                    store.paths[ident] = directory
                    yield ident, rows
            for path in list(self.entries):
                if path not in seen:
                    self.bytes -= self.entries.pop(path)[3]
            return
        visit = (getattr(store, 'iter_records', None) or getattr(store, 'list_records', None)
                 or getattr(store, 'list_recordings', None))
        if callable(visit):
            for record in visit():
                ident, rows = self.read(record)
                if rows:
                    yield ident or record.id, rows
