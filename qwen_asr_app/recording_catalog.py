"""Bounded list snapshots; unchanged recordings never require transcript decoding."""
from __future__ import annotations

from collections import OrderedDict
import copy
import json
from pathlib import Path


def file_stamp(path: Path):
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except FileNotFoundError:
        return None


def recording_stamp(path: Path):
    return tuple(file_stamp(path.parent / name)
                 for name in ('session.json', 'transcript.json', '课程来源.json'))


class RecordingCatalog:
    """Keep only public list fields, with a byte bound independent of library size."""

    def __init__(self, owner, max_bytes=4 * 1024 * 1024):
        self.owner = owner
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.bytes = 0

    def signature(self):
        return tuple(sorted((str(path), recording_stamp(path))
                            for path in self.owner._metadata_paths()))

    def items(self):
        from .storage import RecordingRecord

        paths = self.owner._metadata_paths()
        present = set(paths)
        for path in list(self.entries):
            if path not in present:
                self.bytes -= self.entries.pop(path)[2]
        found = {}
        active = self.owner.active
        for path in paths:
            try:
                stamp = recording_stamp(path)
                cached = self.entries.get(path)
                if active is not None and active.directory == path.parent:
                    # Live counters may have advanced since the last metadata flush.
                    item = active.list_item()
                elif cached is not None and cached[0] == stamp:
                    item = cached[1]
                    self.entries.move_to_end(path)
                else:
                    record = RecordingRecord.load(path.parent)
                    item = record.list_item()
                    del record
                    size = len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
                    previous = self.entries.pop(path, None)
                    if previous is not None:
                        self.bytes -= previous[2]
                    while self.entries and self.bytes + size > self.max_bytes:
                        self.bytes -= self.entries.popitem(last=False)[1][2]
                    if size <= self.max_bytes:
                        self.entries[path] = (stamp, item, size)
                        self.bytes += size
                found[str(item['id'])] = path.parent
                self.owner.paths[str(item['id'])] = path.parent
                yield copy.deepcopy(item)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        # Keep externally restored, moved and removed directories observable.
        self.owner.paths = found
        if active is not None and (active.directory / 'session.json').is_file():
            self.owner.paths[active.id] = active.directory
