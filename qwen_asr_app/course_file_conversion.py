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

OFFICE = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".odp", ".ods", ".rtf"}
TEXT = {".txt", ".md", ".csv", ".tsv"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
SUPPORTED = OFFICE | TEXT | IMAGES | {".pdf"}
ROOT = Path(__file__).resolve().parent.parent
ENGINE = "mineru-hybrid-ocr-v1"


def office_binary():
    candidates = [os.getenv("QWEN_ASR_SOFFICE"), shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice")]
    return next((str(Path(p).expanduser()) for p in candidates if p and Path(p).expanduser().is_file() and os.access(Path(p).expanduser(), os.X_OK)), None)


def capabilities():
    return {"extensions": sorted(SUPPORTED), "office": bool(office_binary()),
            "mineru": bool(find_mineru_binary()), "engine": ENGINE,
            "ocr_note": "MinerU 识别正文、表格与 LaTeX 公式，原页保留供核对。"}


def _text_pdf(source, destination):
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
    body = "<h1>" + html.escape(source.name) + "</h1>" + "".join(
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


def convert(source, destination, progress=lambda stage, percent: None, stop=None):
    import pymupdf as fitz

    check_stopped(stop)
    source, destination = Path(source), Path(destination)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("暂不支持这种格式转换；仍可保存、复制和上传原件")
    if not find_mineru_binary():
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
        result = convert_pdf_to_ai(base, builder_script=ROOT / "scripts" / "build_ai_friendly_pdf.py",
            python_executable=Path(sys.executable), progress=progress, stop=stop)
        with fitz.open(result) as generated:
            pages = len(generated)
            page_texts = [page.get_text().strip() for page in generated]
            total_chars = sum(map(len, page_texts))
            uncertain_pages = [index + 1 for index, text in enumerate(page_texts) if "\ufffd" in text]
        check_stopped(stop)
        result.replace(destination)
    warnings = ["MinerU 已识别正文、表格与 LaTeX 公式；图示和有歧义的公式仍以原页为准。"]
    if uncertain_pages:
        warnings.append("生成 PDF 的这些页含无法还原的字符（�），请对照原页核对：" + "、".join(map(str, uncertain_pages)))
    return {"engine": ENGINE, "pages": pages, "source_pages": source_pages,
            "text_chars": total_chars, "warnings": warnings}
