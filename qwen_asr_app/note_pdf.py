"""Content-addressed, atomic PDF artifacts; only hash-bound reviewed versions publish."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from .durable import atomic_json
from .paths import ROOT
from .review_materials import safe_path, PDF_LOCK

RENDERER='chromium-markdown-katex-v1'
_guard=threading.RLock()


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _reuse_identical_latex_review(folder,output,export,provenance):
    """Carry a visual result across adapter revisions only for identical bytes.

    Compilation and current source validation still run normally. This merely
    avoids asking the reviewer to inspect the exact same PDF a second time
    because the audited compiler adapter gained an unrelated capability.
    """
    destination=output/'review.json'
    if destination.exists():return False  # Never replace a newer review result.
    scope=(provenance or {}).get('learning_scope') or {}
    policy='visual-pages-and-originals-scope-v4' if scope.get('enabled') else 'visual-pages-and-originals-v3'
    pages=export.get('pages')
    if type(pages) is not int or pages<1:return False
    if scope.get('enabled') and not scope.get('sha256'):return False
    outer_keys=('renderer','body_sha256','source_format','latex_body_sha256','template_sha256','pages')
    render_keys=('version','body_sha256','source_sha256','template_sha256','assets','source_refs',
        'renderer','toolchain_sha256','pdf_sha256','pages')
    current_render=export.get('render') or {}
    if not isinstance(current_render,dict):return False
    if any(export.get(key) is None for key in outer_keys) or any(current_render.get(key) is None for key in render_keys):return False
    candidate=output/'candidate.pdf'
    if not candidate.is_file() or candidate.is_symlink() or digest(candidate)!=export.get('pdf_sha256'):return False
    def saved_progress(path):
        try:return len(json.loads((path/'review.json').read_text()).get('viewed_pages',[]))
        except (OSError,ValueError,TypeError,AttributeError):return 0
    for previous in sorted((folder/'pdf').iterdir(),key=lambda path:(-saved_progress(path),path.name)):
        if previous==output or not previous.is_dir() or previous.is_symlink():continue
        paths=[previous/name for name in ('export.json','review.json','candidate.pdf')]
        if any(not path.is_file() or path.is_symlink() for path in paths):continue
        try:
            old=json.loads(paths[0].read_text());review_bytes=paths[1].read_bytes();review=json.loads(review_bytes)
            if not isinstance(old,dict) or not isinstance(review,dict):continue
            if old.get('pdf_sha256')!=export['pdf_sha256'] or digest(paths[2])!=export['pdf_sha256']:continue
            if any(old.get(key)!=export.get(key) for key in outer_keys):continue
            old_render=old.get('render') or {}
            if not isinstance(old_render,dict):continue
            if any(old_render.get(key)!=current_render.get(key) for key in render_keys):continue
            # Older exports predate an explicit binding copy. Their full source,
            # source-time labels, assets and Markdown are all checked above;
            # newer exports additionally retain the exact provenance binding.
            if old.get('source_binding') is not None and old['source_binding']!=export.get('source_binding'):continue
            viewed=review.get('viewed_pages')
            if not isinstance(viewed,list) or any(type(page) is not int for page in viewed):continue
            if review.get('policy')!=policy or review.get('pdf_sha256')!=export['pdf_sha256']:continue
            if review.get('status')=='passed':
                if sorted(viewed)!=list(range(1,pages+1)):continue
            elif review.get('status')=='unreviewed':
                if not viewed or len(viewed)!=len(set(viewed)) or any(page<1 or page>pages for page in viewed):continue
                batches=review.get('batches') or []
                if any(not batch.get('receipt',{}).get('session_id')
                       or batch.get('result',{}).get('viewed_pages')!=batch.get('pages') for batch in batches):continue
                if sorted(page for batch in batches for page in batch.get('pages',[]))!=sorted(viewed):continue
            else:continue
            if scope.get('enabled') and (review.get('learning_scope_sha256')!=scope['sha256']
                    or (review.get('status')=='passed' and review.get('scope_review_passed') is not True)):continue
            copied=dict(review)
            copied['active_batches']={}
            copied['identical_pdf_review_reuse']={'source_fingerprint':previous.name,
                'source_review_sha256':hashlib.sha256(review_bytes).hexdigest(),
                'pdf_sha256':export['pdf_sha256'],'reason':'same-pdf-bytes-and-source-binding'}
            # Publish a complete file only if no reviewer has written a newer
            # result while the matching historical artifacts were checked.
            with tempfile.TemporaryDirectory(prefix='.review-reuse-',dir=output) as temporary:
                ready=Path(temporary)/'review.json';atomic_json(ready,copied)
                try:os.link(ready,destination)
                except FileExistsError:return False
                directory=os.open(output,os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)
            return True
        except (OSError,ValueError,TypeError,KeyError,AttributeError):continue
    return False


def fingerprint(markdown, folder, provenance=None):
    images=[]
    for relative in re.findall(r'!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)',markdown):
        path=safe_path(folder,relative)
        if not path.is_file() or path.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}:raise ValueError('笔记图片不存在或类型不受支持')
        images.append((relative,digest(path)))
    template=ROOT/'scripts/render_review_note.cjs'
    value={'markdown':markdown,'images':images,'provenance':provenance or {},'renderer':RENDERER,
           'template':digest(template),'markdown_renderer':digest(ROOT/'static/js/markdown-reader.js')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _export_markdown_pdf(markdown, folder, provenance=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with _guard:
        ident=fingerprint(markdown,folder,provenance)
        output=folder/'pdf'/ident;output.mkdir(parents=True,exist_ok=True)
        pdf=output/'candidate.pdf';meta=output/'export.json'
        if pdf.exists() and meta.exists() and json.loads(meta.read_text()).get('pdf_sha256')==digest(pdf):return pdf,json.loads(meta.read_text())
        def embed(match):
            path=safe_path(folder,match[1]);mime='image/jpeg' if path.suffix.lower() in {'.jpg','.jpeg'} else 'image/'+path.suffix[1:]
            return match[0].replace(match[1],'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode(),1)
        content=re.sub(r'!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)',embed,markdown)
        modules=[str(ROOT/'scripts/note-export/node_modules'),str(ROOT/'tests/browser/node_modules')]
        if os.getenv('QWEN_ASR_NODE_MODULES'):modules.insert(0,os.environ['QWEN_ASR_NODE_MODULES'])
        modules.extend(str(p) for p in (Path.home()/'.cache/codex-runtimes').glob('*/dependencies/node/node_modules'))
        request=output/'render-request.json';part=output/'candidate.part.pdf'
        atomic_json(request,{'repo_root':str(ROOT),'output':str(part),'markdown':content,'module_roots':modules})
        node=shutil.which('node')
        if not node:raise RuntimeError('PDF 导出需要 Node.js 和 Chromium 渲染器')
        with PDF_LOCK:
            result=subprocess.run([node,str(ROOT/'scripts/render_review_note.cjs'),str(request)],capture_output=True,text=True,timeout=180)
        request.unlink(missing_ok=True)
        if result.returncode:
            part.unlink(missing_ok=True)
            raise RuntimeError('PDF 导出未完成：'+result.stderr[-400:])
        import fitz
        with PDF_LOCK,fitz.open(part) as doc:
            if not len(doc) or any(not p.get_text().strip() and not p.get_images() for p in doc):raise ValueError('PDF 包含空白页面，未发布')
            pages=len(doc)
        with part.open('rb') as stream:os.fsync(stream.fileno())
        os.replace(part,pdf)
        value={'fingerprint':ident,'pdf_sha256':digest(pdf),'pages':pages,'renderer':RENDERER,
               'body_sha256':hashlib.sha256(markdown.encode()).hexdigest(),'render':json.loads(result.stdout)}
        atomic_json(meta,value)
        return pdf,value


def _export_latex_pdf(markdown, folder, provenance):
    from .note_latex import prepare_document,compile_document,toolchain_identity
    from .note_latex_author import _binding
    folder=Path(folder);source_path=folder/'latex/body.tex';manifest_path=folder/'latex/source.json'
    if not source_path.exists() or not manifest_path.exists():
        raise ValueError('当前版本尚未生成LaTeX源稿，请继续生成或升级PDF；原文已保留')
    manifest=json.loads(manifest_path.read_text());body=source_path.read_text()
    if manifest.get('binding')!=_binding(markdown,provenance) or manifest.get('body_sha256')!=hashlib.sha256(body.encode()).hexdigest():
        raise ValueError('正文或资料已改变，LaTeX源稿需要重新生成；未下载旧PDF')
    transcript=json.loads((folder/'transcript.json').read_text()) if (folder/'transcript.json').exists() else {'blocks':[]}
    images=json.loads((folder/'images.json').read_text()) if (folder/'images.json').exists() else []
    prepared=prepare_document(body,folder,transcript,images)
    ident=hashlib.sha256(json.dumps({'source':prepared['source'],'assets':prepared['assets'],
        'provenance':provenance,'renderer':'xelatex-course-v1',
        'compiler_adapter':digest(Path(__file__).with_name('note_latex.py')),
        'toolchain':toolchain_identity()},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    output=folder/'pdf'/ident;output.mkdir(parents=True,exist_ok=True)
    pdf=output/'candidate.pdf';meta=output/'export.json'
    with _guard:
        if pdf.exists() and meta.exists() and json.loads(meta.read_text()).get('pdf_sha256')==digest(pdf):
            cached=json.loads(meta.read_text())
            _reuse_identical_latex_review(folder,output,cached,provenance)
            return pdf,cached
        with PDF_LOCK:
            generated,details=compile_document(body,folder,output/'tex-build',transcript=transcript,images=images)
        part=output/'candidate.part.pdf';shutil.copyfile(generated,part)
        with part.open('rb') as stream:os.fsync(stream.fileno())
        import fitz
        with PDF_LOCK,fitz.open(part) as document:
            if not len(document) or any(not p.get_text().strip() and not p.get_images() for p in document):
                raise ValueError('LaTeX PDF存在空白页；候选已保留，未发布')
            pages=len(document)
        os.replace(part,pdf)
        value={'fingerprint':ident,'pdf_sha256':digest(pdf),'pages':pages,'renderer':'xelatex-course-v1',
            'body_sha256':hashlib.sha256(markdown.encode()).hexdigest(),'source_format':'latex',
            'latex_body_sha256':manifest['body_sha256'],'latex_source_path':'latex/body.tex',
            'template_sha256':prepared['template_sha256'],'source_binding':manifest['binding'],'render':details}
        atomic_json(meta,value)
        _reuse_identical_latex_review(folder,output,value,provenance)
        return pdf,value


def export_pdf(markdown,folder,provenance=None):
    # New course-note flows explicitly bind the direct LaTeX authoring engine.
    # The browser renderer remains available for existing versioned artifacts.
    if (provenance or {}).get('layout_engine')=='xelatex-course-v1':
        return _export_latex_pdf(markdown,folder,provenance)
    return _export_markdown_pdf(markdown,folder,provenance)


def reviewed_pdf(record):
    note=record.metadata.get('ai_note_summary') or {}
    markdown=note.get('summary','');relative=note.get('review_package')
    if not relative:raise ValueError('历史 Markdown 尚未导出并完成独立视觉审查，请在笔记页升级 PDF')
    folder=safe_path(record.directory,relative)
    pdf,export=export_pdf(markdown,folder,note.get('material_version'))
    report=pdf.parent/'review.json'
    if not report.exists():raise ValueError('未完成视觉审查，候选 PDF 已保留，请在笔记页继续审查')
    review=json.loads(report.read_text())
    scope=(note.get('material_version') or {}).get('learning_scope') or {}
    policy='visual-pages-and-originals-scope-v4' if scope.get('enabled') else 'visual-pages-and-originals-v3'
    if scope.get('enabled') and (not scope.get('sha256') or review.get('learning_scope_sha256')!=scope['sha256'] or review.get('scope_review_passed') is not True):
        raise ValueError('当前光电子学版本尚未通过学习要求范围核对，未复用旧版审查结果')
    if review.get('policy')!=policy or review.get('status')!='passed' or review.get('pdf_sha256')!=export['pdf_sha256'] or sorted(review.get('viewed_pages',[]))!=list(range(1,export['pages']+1)):
        raise ValueError('当前版本未完成逐页视觉审查，旧版通过结果未复用')
    return pdf
