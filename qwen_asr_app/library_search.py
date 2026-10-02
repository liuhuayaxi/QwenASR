"""Incremental text index for Chinese substring and multilingual recording search."""
import re


class LibrarySearch:
    def __init__(self):
        self.cache = {}

    def search(self, records, query, category='', offset=0, limit=30):
        terms = [term.casefold() for term in query.split() if term][:12]
        if not terms:
            return {'matches': [], 'total': 0}
        found = []
        live_ids = set()
        for record in records:
            live_ids.add(record.id)
            if category and record.metadata.get('category') != category:
                continue
            stamp = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                          for p in (record.meta_path, record.transcript_json_path))
            cached = self.cache.get(record.id)
            if cached is None or cached[0] != stamp:
                sections = [('title', None, str(record.metadata.get('title') or '')),
                            ('summary', None, str((record.metadata.get('ai_note_summary') or {}).get('summary') or ''))]
                for segment in record.segments:
                    raw = str(segment.get('raw') or '')
                    corrected = str(segment.get('corrected') or '')
                    sections.append(('transcript', float(segment.get('start') or 0), raw + ('\n' + corrected if corrected != raw else '')))
                self.cache[record.id] = (stamp, sections)
            for kind, start, content in self.cache[record.id][1]:
                folded = content.casefold()
                if not all(term in folded for term in terms):
                    continue
                at = folded.find(terms[0])
                snippet = re.sub(r'\s+', ' ', content[max(0, at - 60):at + 180])
                found.append({'recording_id': record.id, 'title': record.metadata.get('title', ''),
                              'category': record.metadata.get('category', ''), 'kind': kind,
                              'start': start, 'snippet': snippet})
        self.cache = {ident: value for ident, value in self.cache.items() if ident in live_ids}
        return {'matches': found[offset:offset + limit], 'total': len(found), 'offset': offset, 'limit': limit}
