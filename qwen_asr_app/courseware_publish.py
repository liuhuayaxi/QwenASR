"""Publish "↗ PPT" links and pre-rendered page images into a sharingnotes checkout.

The site reads ``docs/data/page-links.json`` and ``docs/data/pages/<sha256>/``.
QwenASR already stores the reviewed links beside the note and uploads the AI
PDF as a release asset during ``sharingnotes/scripts/sync_site.py``. This module
fills the two pieces that sync does not: the link file, and one WebP per page
(plus a small preview) for every deck those links still reference.

It reads PDFs and never writes them. Notes are not opened for modification.
Image folders whose sha256 is no longer referenced are removed; a folder whose
manifest is already current is left untouched.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import section_pages as sp

SHA = re.compile(r'[a-f0-9]{64}')
# Same pixels and quality as sharingnotes scripts/publish_viewer_pages.py.
WIDTH, QUALITY, VERSION = 1280, 70, 2
THUMB_WIDTH, THUMB_QUALITY = 320, 45
MARKER = re.compile(r'AI_TRANSCRIPT_PAGE:(\d+)')
_MARKDOWN_LINK = re.compile(r'\[([^[\]]*)\]\([^)]*\)')
SHOWN = {'verified', 'manual'}
COMMIT_MESSAGE = 'Publish courseware page images and PPT jump links'


def heading_plain(text):
    """Visible heading text, matching sharingnotes export_page_links.py."""
    value = str(text or '')
    previous = None
    while value != previous:
        previous, value = value, _MARKDOWN_LINK.sub(r'\1', value)
    return value.strip()


def published_headings(markdown):
    """ATX headings outside fences, in the same order as section_pages.headings."""
    out, fence = [], None
    for line in str(markdown or '').replace('\r\n', '\n').split('\n'):
        stripped = line.lstrip(' ')
        marker = re.match(r'(`{3,}|~{3,})', stripped) if len(line) - len(stripped) <= 3 else None
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence:
            continue
        match = re.match(r'^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$', line)
        if match:
            out.append(match[2].strip())
    return out


def export_page_links(links_dir: Path, data: Path, enabled=True):
    """Build page-links.json from reviewed links and the staged published notes.

    Only ``verified`` and ``manual`` sections are exported, and only when the
    published heading at that index still matches and the AI PDF is in
    resources.json. Uncertain sections stay out, so the site shows no button.
    """
    catalog = json.loads((data / 'catalog.json').read_text(encoding='utf-8'))
    resources = json.loads((data / 'resources.json').read_text(encoding='utf-8'))
    by_sha = {p['sha256']: p for p in resources.get('pdfs', []) if SHA.fullmatch(p.get('sha256') or '')}
    notes = {n['id']: n for n in catalog.get('notes', [])}
    result, report = {'version': 1, 'enabled': bool(enabled), 'notes': {}}, []
    for path in sorted(Path(links_dir).glob('*.json')):
        doc = json.loads(path.read_text(encoding='utf-8'))
        ident = hashlib.sha256((str(doc.get('category', '')) + ':' + str(doc.get('recording_id', ''))).encode()).hexdigest()[:20]
        meta = notes.get(ident)
        if not meta:
            report.append((doc.get('recording_id'), 'not published'))
            continue
        note = json.loads((data / meta['file']).read_text(encoding='utf-8'))
        published = published_headings(note.get('markdown') or '')
        sections, skipped = [], 0
        for section in doc.get('sections', []):
            if section.get('status') not in SHOWN or not section.get('targets'):
                continue
            index = section.get('index')
            if not isinstance(index, int) or index >= len(published) or heading_plain(published[index]) != heading_plain(section.get('heading')):
                skipped += 1
                continue
            targets = []
            for target in section['targets']:
                pdf = by_sha.get(target.get('viewer_sha256') or '')
                if not pdf or target.get('viewer_page_start') is None:
                    continue
                targets.append({
                    'pdf': pdf['id'], 'sha256': pdf['sha256'], 'title': pdf['title'],
                    'page_start': target['page_start'], 'page_end': target['page_end'],
                    'viewer_page_start': target.get('viewer_page_start'),
                    'viewer_page_end': target.get('viewer_page_end'),
                })
            if targets:
                sections.append({'index': index, 'heading': published[index], 'targets': targets})
            else:
                skipped += 1
        result['notes'][ident] = {'version': meta.get('version'), 'mapping_version': doc.get('mapping_version'), 'sections': sections}
        report.append((doc.get('recording_id'), f'{ident}: {len(sections)} links, {skipped} skipped'))
    return result, report


def page_links_text(result) -> str:
    """Stable JSON: sorted notes, sections and targets, no timestamps."""
    notes = {}
    for ident in sorted(result.get('notes') or {}):
        note = result['notes'][ident]
        sections = []
        ordered = sorted(note.get('sections') or [], key=lambda section: (section.get('index'), section.get('heading') or ''))
        for section in ordered:
            targets = sorted(section.get('targets') or [], key=lambda target: (
                str(target.get('pdf') or ''), int(target.get('viewer_page_start') or 0),
                int(target.get('page_start') or 0), str(target.get('sha256') or '')))
            sections.append({'heading': section.get('heading'), 'index': section.get('index'), 'targets': [{
                'page_end': target.get('page_end'), 'page_start': target.get('page_start'), 'pdf': target.get('pdf'),
                'sha256': target.get('sha256'), 'title': target.get('title'),
                'viewer_page_end': target.get('viewer_page_end'), 'viewer_page_start': target.get('viewer_page_start'),
            } for target in targets]})
        notes[ident] = {'mapping_version': note.get('mapping_version'), 'sections': sections, 'version': note.get('version')}
    payload = {'enabled': bool(result.get('enabled')), 'notes': notes, 'version': 1}
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def write_page_links(links_dir: Path, data: Path, enabled=True):
    result, report = export_page_links(links_dir, data, enabled)
    body = page_links_text(result)
    target = Path(data) / 'page-links.json'
    changed = not target.is_file() or target.read_text(encoding='utf-8') != body
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding='utf-8')
    return result, report, changed


def sync_site_publishes_courseware(sharing: Path) -> bool:
    """True when this sharingnotes checkout already runs the page-image hook."""
    script = Path(sharing) / 'scripts' / 'sync_site.py'
    try:
        text = script.read_text(encoding='utf-8')
    except OSError:
        return False
    return ('publish_courseware_view.py' in text and '--sharingnotes' in text
            and '--stage' in text and '--no-commit' in text)


def referenced_decks(data: Path) -> set[str]:
    """Sha256 of AI PDFs that a published button still points at."""
    try:
        links = json.loads((data / 'page-links.json').read_text(encoding='utf-8'))
        resources = json.loads((data / 'resources.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    published = {p.get('sha256') for p in resources.get('pdfs', []) if SHA.fullmatch(p.get('sha256') or '')}
    wanted = set()
    for note in (links.get('notes') or {}).values():
        for section in note.get('sections') or []:
            for target in section.get('targets') or []:
                sha = target.get('sha256') or ''
                if sha in published:
                    wanted.add(sha)
    return wanted


def pages_current(folder: Path, sha: str) -> bool:
    try:
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    pages = manifest.get('pages')
    if manifest.get('version') != VERSION or manifest.get('pdf_sha256') != sha or not isinstance(pages, int) or pages <= 0:
        return False
    return all((folder / f'{prefix}{number}.webp').is_file() for number in range(1, pages + 1) for prefix in ('', 't'))


def render_deck(pdf: Path, folder: Path, sha: str):
    """Write ``<n>.webp`` and ``t<n>.webp`` for one AI PDF. The PDF is only read."""
    import pymupdf
    from PIL import Image
    before = pdf.stat().st_mtime_ns, pdf.stat().st_size
    document = pymupdf.open(pdf)
    temp = folder.with_name(folder.name + '.tmp')
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    try:
        sizes, source_pages, total = [], [], 0
        for page in document:
            zoom = WIDTH / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
            buffer = io.BytesIO()
            image.save(buffer, 'WEBP', quality=QUALITY, method=6)
            (temp / f'{page.number + 1}.webp').write_bytes(buffer.getvalue())
            total += buffer.tell()
            sizes.append([pix.width, pix.height])
            thumb = image.resize((THUMB_WIDTH, max(1, round(image.height * THUMB_WIDTH / image.width))), Image.LANCZOS)
            buffer = io.BytesIO()
            thumb.save(buffer, 'WEBP', quality=THUMB_QUALITY, method=6)
            (temp / f't{page.number + 1}.webp').write_bytes(buffer.getvalue())
            total += buffer.tell()
            marker = MARKER.search(page.get_text() or '')
            source_pages.append(int(marker[1]) if marker else None)
        manifest = {
            'version': VERSION, 'pdf_sha256': sha, 'pages': document.page_count, 'format': 'webp',
            'width': WIDTH, 'quality': QUALITY, 'thumb_width': THUMB_WIDTH, 'sizes': sizes,
            'source_pages': source_pages, 'bytes': total,
        }
        (temp / 'manifest.json').write_text(json.dumps(manifest, separators=(',', ':')) + '\n', encoding='utf-8')
        folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(folder, ignore_errors=True)
        temp.replace(folder)
        temp = None
    finally:
        document.close()
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
    after = pdf.stat().st_mtime_ns, pdf.stat().st_size
    if after != before:
        raise RuntimeError(f'rendering changed the PDF {pdf}')
    return manifest


def sync_viewer_pages(live: Path, dest: Path, decks: dict[str, Path], render=render_deck):
    """Drop unreferenced folders under ``live`` and render missing decks into ``dest``.

    ``decks`` maps sha256 to the local AI PDF. An up-to-date folder under
    ``live`` is skipped, so a republish does not render the same deck again.
    ``dest`` may be ``live`` or a publish stage that is copied over it later.
    """
    live.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    wanted = {sha for sha in decks if SHA.fullmatch(sha)}
    missing_pdf = sorted(sha for sha in wanted if not Path(decks[sha]).is_file())
    if missing_pdf:
        raise ValueError('发布用的 AI PDF 不在本机缓存里，无法生成页图：' + ', '.join(missing_pdf))
    removed = []
    root = live.resolve()
    for entry in list(live.iterdir()) if live.is_dir() else []:
        if not entry.is_dir() or entry.name in wanted:
            continue
        target = entry.resolve()
        if not target.is_relative_to(root):
            continue
        shutil.rmtree(target)
        removed.append(entry.name)
    rendered, skipped = [], []
    for sha in sorted(wanted):
        live_folder = live / sha
        dest_folder = dest / sha
        if pages_current(dest_folder, sha):
            skipped.append(sha)
            continue
        if dest != live and pages_current(live_folder, sha):
            # A fresh sync snapshot may not contain the already-rendered deck.
            # Copy it into the stage so the same publish commit carries the
            # complete viewer data instead of silently omitting the pages.
            if dest_folder.exists():
                shutil.rmtree(dest_folder)
            shutil.copytree(live_folder, dest_folder)
            skipped.append(sha)
            continue
        render(Path(decks[sha]), dest_folder, sha)
        rendered.append(sha)
    return {'rendered': rendered, 'skipped': skipped, 'removed': sorted(removed)}


def asset_index(assets_path: Path | None, media_dir: Path | None = None) -> dict[str, Path]:
    """sha256 → AI PDF, from sync_site's prepared-assets list and its media cache."""
    found = {}
    if assets_path and assets_path.is_file():
        try:
            rows = json.loads(assets_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            rows = []
        for row in rows if isinstance(rows, list) else []:
            sha = row.get('sha256') or ''
            path = Path(str(row.get('path') or ''))
            if SHA.fullmatch(sha) and path.is_file():
                found[sha] = path
    if media_dir and media_dir.is_dir():
        for path in media_dir.glob('pdf-*.pdf'):
            sha = path.name[4:-4]
            if SHA.fullmatch(sha) and sha not in found and path.is_file():
                found[sha] = path
    return found


def sharingnotes_state() -> Path:
    return Path(os.environ.get('SHARINGNOTES_STATE', Path.home() / '.local/state/sharingnotes'))


def _git(sharing: Path, args: list[str], env: dict | None = None) -> str:
    run = subprocess.run(['git', *args], cwd=sharing, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return run.stdout.strip()


def commit_published_view(sharing: Path, *, push: bool, env: dict | None = None) -> str:
    """Commit only docs/data. Leave the repository untouched when nothing changed."""
    if _git(sharing, ['status', '--porcelain', '--', 'docs/data'], env) == '':
        return 'unchanged'
    if _git(sharing, ['branch', '--show-current'], env) != 'main':
        raise ValueError('Automatic publication requires the sharingnotes main branch')
    if _git(sharing, ['diff', '--cached', '--name-only'], env):
        raise ValueError('sharingnotes 已有暂存的修改，页图没有提交')
    _git(sharing, ['add', '--', 'docs/data'], env)
    names = [line for line in _git(sharing, ['diff', '--cached', '--name-only'], env).splitlines() if line]
    if any(not name.startswith('docs/data/') for name in names):
        raise ValueError('页图提交混入了 docs/data 以外的文件')
    _git(sharing, ['diff', '--cached', '--check'], env)
    _git(sharing, ['commit', '-m', COMMIT_MESSAGE], env)
    if push:
        _git(sharing, ['push', 'origin', 'HEAD:main'], env)
    return 'committed'


def publish_courseware_view(sharing: Path, *, stage: Path | None = None, assets_path: Path | None = None,
                            links_dir: Path | None = None, commit: bool = True, push: bool = True,
                            env: dict | None = None, render=render_deck) -> dict:
    """Write page-links and page images, then commit them when asked.

    ``stage`` is sync_site's temporary snapshot. New images are rendered there
    so its existing copy step commits them; folders already on the site stay
    where they are. Orphans are removed from the checkout's ``docs/data/pages``.
    """
    sharing = Path(sharing)
    data = Path(stage) if stage else sharing / 'docs' / 'data'
    live_pages = sharing / 'docs' / 'data' / 'pages'
    links_dir = Path(links_dir) if links_dir else sp.LINKS
    state = sharingnotes_state()
    assets_path = Path(assets_path) if assets_path else state / 'prepared-assets.json'
    result, report, links_changed = write_page_links(links_dir, data, sp.enabled())
    wanted = referenced_decks(data)
    assets = asset_index(assets_path, state / 'media')
    decks = {sha: assets[sha] for sha in wanted if sha in assets}
    missing = sorted(wanted - set(decks))
    if missing:
        raise ValueError('这些课件还没有可渲染的本地 AI PDF：' + ', '.join(missing))
    pages = sync_viewer_pages(live_pages, data / 'pages', decks, render=render)
    committed = 'skipped'
    if commit and stage is None:
        committed = commit_published_view(sharing, push=push, env=env)
    return {'links_changed': links_changed, 'notes': len(result['notes']), 'report': report, 'pages': pages, 'commit': committed}
