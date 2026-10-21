"""Transfer local note figures as real attachments, never localhost URLs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

from .review_materials import safe_path

IMAGE = re.compile(r'!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


def attachment_preview(item):
    return {key: item[key] for key in ('name', 'bytes', 'sha256')}


def _attachment(path, name, limit):
    if path.stat().st_size > limit:
        raise ValueError(f'附件“{name}”超过闪念贝壳单文件 {limit // 1024 // 1024} MB 限制；未忽略附件')
    data = path.read_bytes()
    if len(data) > limit:
        raise ValueError(f'附件“{name}”超过闪念贝壳大小限制，请重新预览')
    if not data:
        raise ValueError(f'附件“{name}”为空')
    return {'name': name, 'data': data, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def prepare_push_assets(record, archive):
    """Return the exact outbound body and a hash-bound attachment manifest.

    IdeaShell images are note-level attachments. Use readable figure markers
    instead of broken relative image URLs, retaining reversible bindings for
    pull. The local illustrated Markdown is never modified by a push.
    """
    archive = dict(archive)
    note = record.metadata.get('ai_note_summary') or {}
    package = note.get('review_package')
    images, bindings, by_path, by_hash, previews = [], [], {}, {}, []

    def replace(match):
        alt, relative = match.groups()
        if relative.startswith(('https://', 'http://')):
            return match[0]
        if not package:
            raise ValueError('笔记引用了本地图片，但缺少图片目录；请先恢复图文笔记后再推送')
        folder = safe_path(record.directory, package)
        path = safe_path(folder, relative)
        if not relative.startswith('assets/') or path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.gif'} or not path.is_file():
            raise ValueError('笔记图片不存在或路径无效：' + relative)
        if relative not in by_path:
            index = len(images) + 1
            name = f'配图-{index:02d}-{path.name}'
            item = _attachment(path, name, MAX_IMAGE_BYTES)
            if item['sha256'] not in by_hash:
                images.append(item)
                previews.append({'name': name, 'url': f'/api/recordings/{quote(record.id, safe="")}/note-assets/{Path(package).name}/{quote(path.name, safe="")}'})
                by_hash[item['sha256']] = (index, name)
            by_path[relative] = by_hash[item['sha256']]
        index, name = by_path[relative]
        marker = f'【配图 {index}：{alt or "课程插图"}；图片附件：{name}】'
        bindings.append({'marker': marker, 'markdown': match[0]})
        return marker

    archive['content'] = IMAGE.sub(replace, archive['content'])
    archive['digest'] = hashlib.sha256(archive['content'].encode()).hexdigest()
    archive['characters'] = len(archive['content'])
    documents, pdf_warning = [], ''
    # Reuse an already published PDF; preview must not generate or review one.
    if images and package:
        expected = note.get('pdf_sha256')
        body_digest = hashlib.sha256(str(note.get('summary') or '').encode()).hexdigest()
        if expected and note.get('review_status') in {'passed', 'skipped'}:
            folder = safe_path(record.directory, package)
            for meta_path in sorted((folder / 'pdf').glob('*/export.json')):
                try:
                    meta = json.loads(meta_path.read_text())
                    if meta.get('pdf_sha256') != expected or meta.get('body_sha256') != body_digest:
                        continue
                    path = safe_path(folder, str(meta_path.parent.relative_to(folder) / 'candidate.pdf'))
                    if not path.is_file():
                        continue
                    item = _attachment(path, '课后复习笔记.pdf', MAX_DOCUMENT_BYTES)
                    if item['sha256'] == expected:
                        documents.append(item)
                        break
                except (OSError, json.JSONDecodeError):
                    continue
        if not documents:
            pdf_warning = '当前笔记尚无可直接上传的已导出 PDF；本次上传正文与全部图片附件'
    manifest = {'images': [attachment_preview(item) for item in images],
                'documents': [attachment_preview(item) for item in documents]}
    archive.update(manifest)
    archive['image_previews'] = previews
    archive['assets_digest'] = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    archive['image_note'] = ('图片保存在笔记附件中，正文配图标记与附件编号对应'
                             + ('；附带 PDF 保留原图文排版' if documents else '')) if images else ''
    archive['pdf_warning'] = pdf_warning
    return archive, images, documents, bindings


def restore_image_markdown(markdown, bindings):
    for binding in bindings or []:
        marker, original = binding.get('marker'), binding.get('markdown')
        if marker and original:
            markdown = markdown.replace(marker, original)
    return markdown
