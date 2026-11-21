"""Sharing Notes note export, ported from sharingnotes ``scripts/export_courses.py``.

Ported byte-for-byte (cleaning, transcript cipher, ID/version rules) so a note
published through the incremental API equals the one the full static export
writes. Contract: sharingnotes-code ``contract/README.md``. Images are read from
the recording folder instead of the loopback HTTP API.
"""
import base64
import hashlib
import json
import re
import struct
import zlib
from pathlib import Path

NOTE_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^\n)]*)\)')
_PATH_PLACEHOLDER = '〔本机路径未公开〕'
# Account homes only. /usr, /tmp, /opt, /var and /etc are course content.
_HOME_PATH = re.compile(r'/(?:Users|home)/[^\s<>\])）]+|(?<![A-Za-z0-9_])~/[^\s<>\])）]+')
# The old drive pattern was [A-Za-z]:\\ plus a run of non-space. That also
# matches TeX units: $n:\text{cm}$, $\omega:\text{rad/s}$ (the final letter of
# \omega), $L_n:\mu\text{m}$ and $f_c:\text{Hz}$.
_DRIVE_PATH = re.compile(r'[A-Za-z]:\\[^\s<>\])）]+')
_TEX_AFTER_DRIVE = re.compile(
    r'[A-Za-z]+\{'
    r'|[A-Za-z]+(?:\\[A-Za-z]+)+\{'
    r'|(?:[A-Za-z]+\\)*[A-Za-z]+\$'
)
_DRIVE_GLUED = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_\\')

def _redact_drive(match):
    token, rest = match.group(0), match.group(0)[3:]
    if _TEX_AFTER_DRIVE.match(rest):
        return token
    previous = match.string[match.start() - 1] if match.start() else ''
    # A letter glued to the drive is a TeX command tail (\omega:\alpha) unless
    # the remainder is actually a directory or a file name.
    if previous in _DRIVE_GLUED and '\\' not in rest and '.' not in rest:
        return token
    return _PATH_PLACEHOLDER

def _redact_local_paths(text):
    text = _HOME_PATH.sub(_PATH_PLACEHOLDER, text)
    return _DRIVE_PATH.sub(_redact_drive, text)

def clean_text(value, published_images=None):
    text = str(value or '')
    placeholders = {}
    def image(match):
        alt, path = match.groups()
        target = (published_images or {}).get(path)
        if not target: return f'〔插图未公开：{alt}〕' if alt else '〔插图未公开〕'
        if not re.fullmatch(r'data/images/[a-f0-9]{64}\.png', target): raise ValueError('Invalid published image path')
        placeholder = f'\ue000PUBLISHED_IMAGE_{len(placeholders)}\ue001'
        placeholders[placeholder] = f'![{clean_text(alt)}]({target})'
        return placeholder
    text = NOTE_IMAGE.sub(image, text)
    text = re.sub(r'<(audio|video|iframe|object|script|style)\b[^>]*>[\s\S]*?</\1\s*>', '', text, flags=re.I)
    text = re.sub(r'<(?:img|source|embed)\b[^>]*>', '', text, flags=re.I)
    text = re.sub(r'(?<!!)\[([^\]]+)\]\((?!#)[^\n]*?\)', r'\1', text)
    text = re.sub(r'(?:https?://|file://)[^\s<>\])）]+', '〔外部地址未公开〕', text)
    text = _redact_local_paths(text)
    text = re.sub(r'(?<![A-Za-z0-9])[Bb]\d{8}(?!\d)', '〔学号已隐藏〕', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '〔联系电话已隐藏〕', text)
    text = re.sub(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '〔邮箱已隐藏〕', text)
    for placeholder, markup in placeholders.items(): text = text.replace(placeholder, markup)
    return text.strip()

TRANSCRIPT_SALT = 'sharingnotes/transcript/v1'

def transcript_seed(note_id, section, part):
    """FNV-1a (32-bit) of the key label; mirrored in docs/cipher.js."""
    value = 0x811c9dc5
    for byte in f'{TRANSCRIPT_SALT}|{note_id}|{section}|{part}'.encode():
        value = ((value ^ byte) * 0x01000193) & 0xffffffff
    return value or 0x9e3779b9

def transcript_keystream(seed, length):
    words, state = [], seed
    for _ in range((length + 3) // 4):
        state ^= (state << 13) & 0xffffffff
        state ^= state >> 17
        state ^= (state << 5) & 0xffffffff
        words.append(state)
    return struct.pack(f'<{len(words)}I', *words)[:length]

def transcript_cipher(text, note_id, section, part):
    """Obfuscate classroom transcript text for the public site.

    UTF-8 bytes are raw-deflated (keeps the public files small), XORed with an
    xorshift32 stream keyed by note, passage and part, then base64 encoded. This only deters casual reading of the public
    JSON: the browser holds the same algorithm in docs/cipher.js.
    """
    if not str(text): return ''
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = packer.compress(str(text).encode()) + packer.flush()
    stream = transcript_keystream(transcript_seed(note_id, section, part), len(data))
    mixed = (int.from_bytes(data, 'little') ^ int.from_bytes(stream, 'little')).to_bytes(len(data), 'little')
    return base64.b64encode(mixed).decode('ascii')

def published_sources(note_id, sources):
    """Public note JSON keeps passage ids and times; the text is obfuscated."""
    result = []
    for source in sources:
        item = {k: v for k, v in source.items() if k != 'text'}
        item['cipher'] = transcript_cipher(source['text'], note_id, 'source-' + source['id'], 'note')
        result.append(item)
    return result

def png_dimensions(payload):
    if not payload.startswith(b'\x89PNG\r\n\x1a\n') or payload[12:16] != b'IHDR': raise ValueError('Image is not PNG')
    offset, ended = 8, False
    while offset + 12 <= len(payload):
        length = struct.unpack('>I', payload[offset:offset+4])[0]
        stop = offset + 12 + length
        if stop > len(payload): raise ValueError('Truncated PNG')
        chunk = payload[offset+4:offset+8+length]
        expected = struct.unpack('>I', payload[offset+8+length:stop])[0]
        if zlib.crc32(chunk) & 0xffffffff != expected: raise ValueError('PNG integrity check failed')
        offset = stop
        if chunk[:4] == b'IEND': ended = True; break
    if not ended: raise ValueError('PNG has no end marker')
    width, height = struct.unpack('>II', payload[16:24])
    if not 0 < width <= 100000 or not 0 < height <= 100000: raise ValueError('Invalid PNG dimensions')
    return width, height

def source_texts(session, note):
    segments = session.get('segments') or []
    by_id = {str(s.get('id')): s for s in segments}
    mapping = note.get('source_map') or {}
    if not isinstance(mapping, dict): return []
    results = []
    for ident, item in mapping.items():
        if not isinstance(item, dict) or not re.fullmatch(r'\d+', str(ident)): continue
        selected = [by_id[str(i)] for i in item.get('segment_ids', []) if str(i) in by_id]
        if not selected: continue
        text = '\n\n'.join(clean_text(s.get('corrected') or s.get('raw')) for s in selected)
        if not text.strip(): continue
        results.append({'id': str(ident), 'start': max(0, float(item.get('start') or 0)),
                        'end': max(0, float(item.get('end') or 0)), 'text': text,
                        'corrected': all(bool(s.get('corrected')) for s in selected)})
    return results

SOURCE_LINK = re.compile(r'\[([^\]\n]*)\]\(#source-([^)\s]*)\)')

def unlink_unresolved_citations(markdown, source_ids):
    """AI notes sometimes describe the citation format with a template such as
    [原文 Sxx](#source-xx). Such a link has no classroom passage, so it is kept as
    plain, non-clickable label text. Real citations are left untouched."""
    ids = {str(i) for i in source_ids}
    return SOURCE_LINK.sub(lambda m: m.group(0) if m.group(2) in ids else m.group(1), markdown)

def public_note(session, published_images=None, images=None):
    note = session.get('ai_note_summary') or {}
    if session.get('status') != 'completed' or not str(note.get('summary') or '').strip(): return None
    course = clean_text(session.get('category'))
    ident = hashlib.sha256((course + ':' + str(session['id'])).encode()).hexdigest()[:20]
    sources = source_texts(session, note)
    markdown = unlink_unresolved_citations(clean_text(note['summary'], published_images), [s['id'] for s in sources])
    chapter = clean_text(session.get('chapter')) if isinstance(session.get('chapter'), str) else ''
    declared = session.get('chapters') if isinstance(session.get('chapters'), list) else []
    chapters = list(dict.fromkeys(clean_text(value) for value in declared if isinstance(value, str) and clean_text(value)))
    if not chapters and chapter: chapters = [chapter]
    if not chapter and chapters: chapter = ' · '.join(chapters)
    declared_sections = (session.get('chapter_info') or {}).get('sections') or {}
    headings = set(re.findall(r'^#{1,6}[ 	]+(.+?)[ 	]*$', markdown, re.M))
    sections = {}
    if isinstance(declared_sections, dict):
        for raw_label, raw_heading in declared_sections.items():
            if isinstance(raw_label, str) and isinstance(raw_heading, str):
                label, heading = clean_text(raw_label), clean_text(raw_heading)
                if label in chapters and heading in headings:
                    sections[label] = heading
    version_fields = [markdown, sources, chapter, chapters]
    if sections:
        version_fields.append(sections)
    version = hashlib.sha256(json.dumps(version_fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    date = str(session.get('recorded_at') or session.get('started_at') or '')[:10]
    return {'id': ident, 'course': course, 'title': clean_text(session.get('title') or course), 'date': date,
            'chapter': chapter, 'chapters': chapters,
            **({'chapter_sections': sections} if sections else {}),
            'abstract': clean_text(note.get('abstract') or '')[:700], 'markdown': markdown, 'sources': sources,
            'sourceBodySha256': hashlib.sha256(str(note['summary']).encode()).hexdigest(),
            'version': version,
            'wordCount': len(re.sub(r'\s', '', markdown)), 'images': images or [], 'imageCount': len(images or [])}



def read_note_asset(directory, package, name):
    """PNG bytes of a note figure, as the ``note-assets`` route serves them."""
    from .review_materials import safe_path
    path = safe_path(Path(directory), f'{package}/assets/{name}')
    if not path.is_file():
        raise ValueError(f'Embedded image is missing: {name}')
    return path.read_bytes()


def export_images(session, read_asset=None):
    """Local counterpart of the exporter's ``export_images``: same checks and
    mapping, returns ``(mapping, images, blobs)`` with ``blobs[sha256] = bytes``."""
    note = session.get('ai_note_summary') or {}
    references = list(dict.fromkeys(match[2] for match in NOTE_IMAGE.finditer(str(note.get('summary') or ''))))
    if not references:
        return {}, [], {}
    package = str(note.get('review_package') or '')
    if not re.fullmatch(r'\.review-notes/(?:legacy-)?[a-f0-9]{24}', package):
        raise ValueError('Embedded images have no valid note package')
    read_asset = read_asset or (lambda name: read_note_asset(session.get('directory') or '', package, name))
    mapping, images, blobs = {}, [], {}
    for reference in references:
        if not re.fullmatch(r'assets/fig-[a-f0-9]{16}\.png', reference):
            raise ValueError('Unsupported embedded image reference')
        payload = read_asset(reference.split('/')[-1])
        if len(payload) > 25 * 1024 * 1024:
            raise ValueError('One embedded image exceeds 25 MiB')
        width, height = png_dimensions(payload)
        digest = hashlib.sha256(payload).hexdigest()
        public_path = f'data/images/{digest}.png'
        mapping[reference] = public_path
        if not any(item['src'] == public_path for item in images):
            images.append({'src': public_path, 'sha256': digest, 'width': width, 'height': height})
        blobs[digest] = payload
    return mapping, images, blobs


def export_note(session, read_asset=None):
    """One published note, exactly as the full exporter writes it.

    Returns ``None`` for unpublishable sessions, else a dict with ``note`` (with
    ciphered sources), ``body`` (the compact JSON bytes of the note file),
    ``catalogEntry`` and ``images`` (PNG bytes by sha256).
    """
    if session.get('status') != 'completed' or not (session.get('ai_note_summary') or {}).get('summary'):
        return None
    mapping, images, blobs = export_images(session, read_asset)
    note = public_note(session, mapping, images)
    if not note:
        return None
    note = {**note, 'sources': published_sources(note['id'], note['sources'])}
    filename = f"notes/{note['id']}-{note['version'][:12]}.json"
    body = json.dumps(note, ensure_ascii=False, separators=(',', ':')).encode()
    entry = {k: note[k] for k in ['id', 'course', 'title', 'chapter', 'chapters', 'date', 'abstract', 'version', 'wordCount', 'imageCount']}
    entry.update(file=filename, sourceCount=len(note['sources']),
                 lessonAt=str(session.get('recorded_at') or session.get('started_at') or note['date']))
    return {'note': note, 'body': body, 'catalogEntry': entry, 'images': blobs}
