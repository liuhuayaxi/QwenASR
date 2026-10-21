"""Read metadata from one bound CLI session; never expose model reasoning."""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re
import time


class SessionActivity:
    def __init__(self, session_id, root=None):
        if not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',str(session_id)):
            raise ValueError('invalid session identity')
        self.path=(root or Path.home()/'.gemini/antigravity-cli/brain')/session_id/'.system_generated/logs/transcript_full.jsonl'
        self.offset=0;self.tail=b'';self.checked=0;self.seen=set();self.pending=deque()
        self.read_files=set();self.images=set();self.pages=set()
        self.thinking_chars=0;self.output_chars=0;self.model_turns=0;self.tools=0
        self.phase='processing';self.last_event_at=0

    def refresh(self):
        now=time.monotonic()
        if now-self.checked<1:return
        self.checked=now
        try:
            stat=self.path.stat()
            if stat.st_size<=self.offset:return
            with self.path.open('rb') as stream:
                stream.seek(self.offset);chunk=stream.read(512*1024)
            self.offset+=len(chunk)
            lines=(self.tail+chunk).split(b'\n');self.tail=lines.pop()
            for line in lines:
                try:event=json.loads(line)
                except ValueError:continue
                key=event.get('step_index')
                if key is None or key in self.seen:continue
                self.seen.add(key)
                kind=event.get('type')
                if kind=='PLANNER_RESPONSE':
                    self.model_turns+=1
                    self.thinking_chars+=len(event.get('thinking') or '')
                    self.output_chars+=len(event.get('content') or '')
                    self.phase='receiving' if event.get('content') else 'thinking' if event.get('thinking') else 'processing'
                    for tool in event.get('tool_calls') or []:
                        self.tools+=1
                        if tool.get('name') in {'view_file','read_file'}:
                            path=str((tool.get('args') or {}).get('AbsolutePath' if tool['name']=='view_file' else 'Path') or '')
                            self.pending.append(path);self.phase='reading'
                elif kind=='GENERIC' and self.pending:
                    path=self.pending.popleft()
                    if event.get('status')=='DONE':
                        self.read_files.add(path)
                        if event.get('media'):
                            self.images.add(path)
                            match=re.search(r'/pages/[^/]+/page-(\d+)\.png$',path)
                            if match:self.pages.add(int(match[1]))
                    self.phase='processing'
                # The file's last modification is evidence of a real event,
                # including thinking-only turns. No text is returned to the UI.
                self.last_event_at=stat.st_mtime
        except (OSError,ValueError,TypeError):return

    def snapshot(self):
        self.refresh()
        return {'phase':self.phase,'thinking_chars':self.thinking_chars,'session_output_chars':self.output_chars,
            'model_turns':self.model_turns,'tool_calls':self.tools,'read_files':len(self.read_files),
            'image_reads':len(self.images),'opened_pages':sorted(self.pages),'last_event_at':self.last_event_at}
