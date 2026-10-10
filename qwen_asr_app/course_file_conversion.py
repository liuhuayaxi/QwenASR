"""Prepare course files as PDFs, then use the workbench's MinerU pipeline."""
from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .pdf_ai import convert_pdf_to_ai, find_mineru_binary, _run_checked
from .conversion_control import check_stopped
from .mineru_settings import mineru_settings, normalize_mineru_settings

OFFICE = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".odp", ".ods", ".rtf"}
TEXT = {".txt", ".md", ".csv", ".tsv"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
SUPPORTED = OFFICE | TEXT | IMAGES | {".pdf"}
ROOT = Path(__file__).resolve().parent.parent
ENGINE = "mineru-4-v1"


def office_binary():
    candidates = [os.getenv("QWEN_ASR_SOFFICE"), shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice")]
    return next((str(Path(p).expanduser()) for p in candidates if p and Path(p).expanduser().is_file() and os.access(Path(p).expanduser(), os.X_OK)), None)


def capabilities():
    from .inference_providers import inference_settings
    mode = inference_settings.for_task('mineru')['mineru_provider']
    return {"extensions": sorted(SUPPORTED), "office": bool(office_binary()),
            "mineru": mode != "local" or bool(find_mineru_binary()), "engine": ENGINE,
            "native_office": mode != 'local', "office_rendering": bool(office_binary()),
            "ocr_note": "PDF 原页保留；远程原生 Office 使用 Flash，转写 PDF 与原件版面分别标注。"}


def _text_pdf(source, destination, title=None):
    import pymupdf as fitz

    raw = source.read_bytes()
    encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise ValueError("文本编码无法识别，请先保存为 UTF-8")
    if "\x00" in text:
        raise ValueError("文本含二进制内容，请先保存为 UTF-8")
    # Preserve literal Markdown / LaTeX and readable text rather than fetching links.
    # Bound long lines (including URLs / formulas) so they cannot overflow a page.
    import textwrap
    lines = [part for line in text.splitlines() for part in (textwrap.wrap(line.expandtabs(4), width=70, replace_whitespace=False, drop_whitespace=False) or [""])]
    body = "<h1>" + html.escape(title or source.name) + "</h1>" + "".join(
        "<p>" + html.escape(line) + "</p>" for line in lines
    )
    story = fitz.Story(html=body, user_css="body{font-size:11pt;line-height:1.5;}p{margin:0 0 5pt;overflow-wrap:break-word;}h1{font-size:18pt;}")
    writer = fitz.DocumentWriter(str(destination))
    try:
        more = True
        while more:
            device = writer.begin_page(fitz.paper_rect("a4"))
            more, _ = story.place(fitz.Rect(42, 42, 553, 800))
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()


def _image_pdf(source, destination):
    import pymupdf as fitz

    from PIL import Image, ImageOps, ImageSequence
    doc = fitz.open()
    try:
        with Image.open(source) as image:
            for frame in ImageSequence.Iterator(image):
                frame = ImageOps.exif_transpose(frame).convert("RGB")
                import io
                stream = io.BytesIO()
                frame.save(stream, format="PNG")
                width, height = frame.size
                scale = min(1, 842 / max(width, height))
                page = doc.new_page(width=width * scale, height=height * scale)
                page.insert_image(page.rect, stream=stream.getvalue())
        doc.save(destination, deflate=True)
    finally:
        doc.close()


def convert(source, destination, progress=lambda stage, percent: None, stop=None, settings=None, inference_config=None, receipt_path=None, local_guard=None):
    import pymupdf as fitz

    check_stopped(stop)
    source, destination = Path(source), Path(destination)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("暂不支持这种格式转换；仍可保存、复制和上传原件")
    from .inference_providers import inference_settings
    provider = inference_settings.for_task('mineru', inference_config)
    if provider['mineru_provider'] == 'local':
        settings = normalize_mineru_settings(settings) if settings is not None else mineru_settings.read()
    if suffix in {'.docx', '.pptx', '.xlsx', '.odt', '.odp', '.ods', '.rtf', '.csv', '.tsv'} and provider['mineru_provider'] != 'local':
        # Native Flash parsing is independent of optional Office page rendering.
        from .inference_providers import RemoteMinerUProvider
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            from .remote_operations import accepted_result_guard
            with accepted_result_guard(receipt_path), tempfile.TemporaryDirectory(prefix='.native-', dir=destination.parent) as raw:
                work = Path(raw)
                inference = RemoteMinerUProvider().parse(source, work/'result', provider, progress, stop, receipt_path)
                markdown = next((work/'result').glob('*.md'))
                rendered = work/'native-text.pdf'
                _text_pdf(markdown, rendered, title=source.name)
                with fitz.open(rendered) as document:
                    pages = len(document)
                    document.embfile_add('mineru-result.zip', (work/'result/result.zip').read_bytes())
                    document.embfile_add('mineru-transcript.md', markdown.read_bytes())
                    packaged = work/'native-packaged.pdf'
                    document.save(packaged, deflate=True)
                check_stopped(stop)
                packaged.replace(destination)
                return {'engine': ENGINE, 'pages': pages, 'source_pages': None, 'text_chars': len(markdown.read_text()),
                    'warnings': ['Office 原件由 Windows 原生 Flash 解析；此 PDF 是转写排版，原件版面未渲染。图表估计内容需要核对原件。'],
                    'mineru_settings': settings, 'inference': inference, 'representation': 'native-transcript',
                    'visual_original_preserved': False}
        except Exception as exc:
            if provider['mineru_provider'] == 'remote_only' or not getattr(exc, 'proven_not_accepted', False):
                raise
            from .remote_operations import mark_local_fallback
            mark_local_fallback(receipt_path)
            import contextlib
            with local_guard or contextlib.nullcontext():
                result = convert(source, destination, progress, stop, settings, {**provider, 'mineru_provider': 'local'}, receipt_path)
            result['inference'].update(fallback=True, fallback_reason=type(exc).__name__)
            return result
    if provider['mineru_provider'] == 'local' and not find_mineru_binary():
        raise ValueError("未找到 MinerU，请先配置工作台原有的 AI 友好 PDF 功能")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".convert-", dir=destination.parent) as raw:
        work = Path(raw)
        base = work / (source.stem + ".pdf")
        progress("正在准备 PDF 页面", 1)
        if suffix == ".pdf":
            shutil.copyfile(source, base)
        elif suffix in TEXT:
            _text_pdf(source, base)
        elif suffix in IMAGES:
            _image_pdf(source, base)
        else:
            binary = office_binary()
            if not binary:
                raise ValueError("Office 转 PDF 需要 LibreOffice；安装后可重新转换")
            profile = (work / "office-profile").as_uri()
            _run_checked([binary, f"-env:UserInstallation={profile}", "--headless",
                "--convert-to", "pdf", "--outdir", str(work), str(source)],
                work / "office.log", stop=stop, timeout=300)
            produced = work / (source.stem + ".pdf")
            if not produced.exists():
                raise ValueError("Office 转换失败，请检查文件能否正常打开")
            if produced != base:
                produced.replace(base)
        doc = fitz.open(base)
        try:
            if doc.needs_pass:
                raise ValueError("PDF 已加密，请先解密再转换")
            if not len(doc):
                raise ValueError("文件中没有可转换的页面")
            source_pages = len(doc)
        finally:
            doc.close()
        inference = {}
        result = convert_pdf_to_ai(base, builder_script=ROOT / "scripts" / "build_ai_friendly_pdf.py",
            python_executable=Path(sys.executable), progress=progress, stop=stop, settings=settings,
            inference_metadata=inference, inference_config=provider, receipt_path=receipt_path, local_guard=local_guard)
        with fitz.open(result) as generated:
            pages = len(generated)
            page_texts = [page.get_text().strip() for page in generated]
            total_chars = sum(map(len, page_texts))
            uncertain_pages = [index + 1 for index, text in enumerate(page_texts) if "\ufffd" in text]
        check_stopped(stop)
        result.replace(destination)
    warnings = ["MinerU 已生成转写；科学符号、表格单元格及图表估计值未逐项核验。原图为核对依据，图表估计值不能作为事实数据。"]
    if uncertain_pages:
        warnings.append("生成 PDF 的这些页含无法还原的字符（�），请对照原页核对：" + "、".join(map(str, uncertain_pages)))
    return {"engine": ENGINE, "pages": pages, "source_pages": source_pages,
            "text_chars": total_chars, "warnings": warnings, "mineru_settings": settings, "inference": inference}
