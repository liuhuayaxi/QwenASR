"""Courseware for incremental publication: AI PDFs, PPT jump links and page images.

For one exported note this builds the upsert's ``materials``, ``pageLinks`` and
``pageSets`` plus the blobs they reference, all addressed by content hash, so
the publisher only uploads a PDF or page image the server does not have yet.
The same rules as the static path apply: material IDs from ``sync_site.prepare``,
only verified/manual links from ``courseware_publish.export_page_links``, and
page images rendered once by ``courseware_publish.render_deck``.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from . import courseware_publish as cp
from . import section_pages as sp
from .durable import atomic_json
from .sharingnotes_export import clean_text
from .sharingnotes_publish import STATE_DIR

SHA = re.compile(r'[a-f0-9]{64}')


def material_id(course, asset_id):
    return hashlib.sha256((str(course) + ':' + str(asset_id)).encode()).hexdigest()[:20]


def _file_sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


class CoursewareSource:
    """``publisher.courseware`` hook: ``(exported, session) -> build_upsert kwargs + blobs``."""

    def __init__(self, course_files, links_dir=None, cache_dir=None, enabled=None, render=cp.render_deck,
                 local_pages=None):
        self.course_files = course_files
        self.links_dir = Path(links_dir) if links_dir else sp.LINKS
        self.cache_dir = Path(cache_dir) if cache_dir else STATE_DIR / 'courseware'
        self.enabled = sp.enabled if enabled is None else enabled
        self.render = render
        self.local_pages = Path(local_pages) if local_pages else sp.PAGES
        self.lock = threading.Lock()
        self._hashes = None

    # ------------------------------------------------------------ hashing
    def _hash_path(self):
        return self.cache_dir / 'hashes.json'

    def digest(self, path):
        """sha256 of a file, cached by inode/size/mtime so unchanged PDFs are not re-read."""
        path = Path(path)
        stat = path.stat()
        key, stamp = str(path), [stat.st_ino, stat.st_size, stat.st_mtime_ns]
        with self.lock:
            if self._hashes is None:
                try:
                    self._hashes = json.loads(self._hash_path().read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    self._hashes = {}
            cached = self._hashes.get(key)
            if cached and cached.get('stamp') == stamp:
                return cached['sha256']
        value = _file_sha(path)
        after = path.stat()
        if [after.st_ino, after.st_size, after.st_mtime_ns] != stamp:
            raise ValueError(f'{path.name} 在读取时被修改，下次再发布')
        with self.lock:
            self._hashes[key] = {'stamp': stamp, 'sha256': value}
            if path.suffix.lower() == '.pdf':  # page images are cheap; only persist PDFs
                atomic_json(self._hash_path(), {k: v for k, v in self._hashes.items() if k.lower().endswith('.pdf')})
        return value

    # ---------------------------------------------------------- materials
    def _pdf(self, folder, item):
        conversion = item.get('conversion') or {}
        if conversion.get('status') != 'completed':
            return None
        relative = conversion.get('relative_path') or f"{item['id']}/PDF/{conversion.get('name') or ''}"
        root = Path(folder).resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != '.pdf' or not path.is_file():
            return None
        return path

    def _materials(self, course):
        """Every completed AI PDF of a course: id -> (material, path)."""
        try:
            folder = self.course_files.folder(course)
            items = self.course_files.list(course)
        except (OSError, ValueError):
            return {}
        found = {}
        for item in items:
            path = self._pdf(folder, item)
            if path is None:
                continue
            with path.open('rb') as stream:
                if not stream.read(8).startswith(b'%PDF-'):
                    raise ValueError(f'课件 {clean_text(item.get("name"))} 的 AI PDF 签名无效')
            conversion = item['conversion']
            pages = conversion.get('pages')
            material = {
                'id': material_id(course, item['id']), 'course': clean_text(course),
                'title': clean_text(item.get('name')), 'kind': clean_text(item.get('kind') or '课程资料'),
                'pages': pages if type(pages) is int and pages >= 0 else 0,
                'updatedAt': conversion.get('updated_at') or conversion.get('created_at') or '',
                'sha256': self.digest(path), 'bytes': path.stat().st_size,
            }
            material['url'] = f"/media/pdf-{material['sha256']}.pdf"
            source_pages = conversion.get('source_pages')
            if type(source_pages) is int and source_pages > 0:
                material['source_pages'] = source_pages
            found[item['id']] = (material, path)
        return found

    # --------------------------------------------------------- page links
    def _links(self, session, note, by_sha):
        if not self.enabled():
            return [], None
        try:
            doc = json.loads((self.links_dir / f"{session.get('id')}.json").read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return [], None
        if note.get('sourceBodySha256') and note['sourceBodySha256'] != doc.get('note_body_sha'):
            return [], None  # links were reviewed against an older body
        published = cp.published_headings(note.get('markdown') or '')
        sections = []
        for section in doc.get('sections', []):
            if section.get('status') not in cp.SHOWN or not section.get('targets'):
                continue
            index = section.get('index')
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(published) \
                    or cp.heading_plain(published[index]) != cp.heading_plain(section.get('heading')):
                continue
            targets = []
            for target in section['targets']:
                material = by_sha.get(target.get('viewer_sha256') or '')
                if not material or target.get('viewer_page_start') is None:
                    continue
                targets.append({'pdf': material['id'], 'sha256': material['sha256'], 'title': material['title'],
                                'page_start': target['page_start'], 'page_end': target['page_end'],
                                'viewer_page_start': target['viewer_page_start'],
                                'viewer_page_end': target.get('viewer_page_end')})
            if targets:
                sections.append({'index': index, 'heading': published[index], 'targets': targets})
        return sections, doc.get('mapping_version')

    # --------------------------------------------------------- page images
    def _page_folder(self, sha, pdf_path):
        local = self.local_pages / sha
        if cp.pages_current(local, sha):
            return local
        folder = self.cache_dir / 'pages' / sha
        if not cp.pages_current(folder, sha):
            self.render(Path(pdf_path), folder, sha)
        return folder

    def _page_set(self, sha, pdf_path, blobs):
        folder = self._page_folder(sha, pdf_path)
        manifest_path = folder / 'manifest.json'
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        manifest_sha = hashlib.sha256(raw).hexdigest()
        blobs[manifest_sha] = ('manifest', 'application/json', manifest_path)
        files = []
        for number in range(1, manifest['pages'] + 1):
            full, thumb = folder / f'{number}.webp', folder / f't{number}.webp'
            full_sha, thumb_sha = self.digest(full), self.digest(thumb)
            blobs[full_sha] = ('page', 'image/webp', full)
            blobs[thumb_sha] = ('page', 'image/webp', thumb)
            files.append({'page': number, 'sha256': full_sha, 'thumbnailSha256': thumb_sha})
        return {'manifest': manifest, 'manifestSha256': manifest_sha, 'files': files}

    # ---------------------------------------------------------------- hook
    def __call__(self, exported, session):
        note = exported['note']
        course = str(session.get('category') or '')
        available = self._materials(course)
        used = ((session.get('ai_note_summary') or {}).get('material_version') or {}).get('course_files') or {}
        chosen = {asset: available[asset] for asset in used if asset in available}
        by_sha = {material['sha256']: material for material, _ in available.values()}
        sections, mapping_version = self._links(session, note, by_sha)
        # A jump link may point at a deck the note did not list; it is still this note's material.
        linked = {t['sha256'] for s in sections for t in s['targets']}
        for asset, (material, path) in available.items():
            if material['sha256'] in linked and asset not in chosen:
                chosen[asset] = (material, path)
        blobs, page_sets, paths = {}, [], {}
        for material, path in chosen.values():
            blobs[material['sha256']] = ('pdf', 'application/pdf', path)
            paths[material['sha256']] = path
        for sha in sorted(linked):
            page_set = self._page_set(sha, paths[sha], blobs)
            page_sets.append(page_set)
            for material, _ in chosen.values():
                if material['sha256'] == sha:
                    material['pages'] = page_set['manifest']['pages']
                    material['pageImages'] = True
        materials = [material for material, _ in chosen.values()]
        page_links = {'version': note['version'], 'mapping_version': mapping_version, 'sections': sections,
                      'materials': [m['id'] for m in materials]}
        return {'materials': materials, 'page_links': page_links, 'page_sets': page_sets,
                'pdfs': list(dict.fromkeys(m['sha256'] for m in materials)),
                'pages': list(dict.fromkeys(h for p in page_sets for f in p['files']
                                            for h in (f['sha256'], f['thumbnailSha256']))),
                'blobs': blobs}
