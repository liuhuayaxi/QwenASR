"""Prepare course files as PDFs, then use the workbench's MinerU pipeline."""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .pdf_ai import convert_pdf_to_ai, find_mineru_binary, _run_checked
from .native_pdf import pdf_operation
from .conversion_control import check_stopped
from .mineru_settings import mineru_settings, normalize_mineru_settings

OFFICE = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".odp", ".ods", ".rtf"}
TEXT = {".txt", ".md", ".csv", ".tsv"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
SUPPORTED = OFFICE | TEXT | IMAGES | {".pdf"}
ROOT = Path(__file__).resolve().parent.parent
ENGINE = "mineru-4-v1"
_RETRYABLE_FILE_ERRNOS = {11, getattr(errno, "EDEADLK", 35), getattr(errno, "EAGAIN", 11)}


def _copy_file_retry(source: Path, destination: Path) -> None:
    """Retry transient macOS File Provider ``Resource deadlock avoided`` reads."""
    for attempt in range(6):
        try:
            shutil.copyfile(source, destination)
            return
        except OSError as exc:
            if exc.errno not in _RETRYABLE_FILE_ERRNOS or attempt == 5:
                raise
            time.sleep(min(4.0, 0.35 * (2 ** attempt)))


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
    pdf_operation('text_pdf', path=str(source), output=str(destination), title=title)


def _image_pdf(source, destination):
    pdf_operation('image_pdf', path=str(source), output=str(destination))


def convert(source, destination, progress=lambda stage, percent: None, stop=None, settings=None, inference_config=None, receipt_path=None, local_guard=None):
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
                packaged = work/'native-packaged.pdf'
                pages = pdf_operation('package', path=str(rendered), output=str(packaged),
                    attachments={'mineru-result.zip': str(work/'result/result.zip'),
                                 'mineru-transcript.md': str(markdown)})
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
        progress("正在准备 PDF 页面", None)
        if suffix == ".pdf":
            _copy_file_retry(source, base)
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
        source_pages = pdf_operation('validate', path=str(base))
        inference = {}
        result = convert_pdf_to_ai(base, builder_script=ROOT / "scripts" / "build_ai_friendly_pdf.py",
            python_executable=Path(sys.executable), progress=progress, stop=stop, settings=settings,
            inference_metadata=inference, inference_config=provider, receipt_path=receipt_path, local_guard=local_guard)
        stats = pdf_operation('text_stats', path=str(result))
        pages, total_chars, uncertain_pages = stats['pages'], stats['text_chars'], stats['uncertain_pages']
        check_stopped(stop)
        result.replace(destination)
    warnings = ["MinerU 已生成转写；科学符号、表格单元格及图表估计值未逐项核验。原图为核对依据，图表估计值不能作为事实数据。"]
    if uncertain_pages:
        warnings.append("生成 PDF 的这些页含无法还原的字符（�），请对照原页核对：" + "、".join(map(str, uncertain_pages)))
    return {"engine": ENGINE, "pages": pages, "source_pages": source_pages,
            "text_chars": total_chars, "warnings": warnings, "mineru_settings": settings, "inference": inference}
