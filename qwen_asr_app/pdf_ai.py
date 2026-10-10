from __future__ import annotations

import os
import json
import re
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
import contextlib
from pathlib import Path
from typing import Callable, Iterable
from .mineru_settings import RUNTIME_ROOT, mineru_settings, normalize_mineru_settings, runtime_info
from .conversion_control import check_stopped


ProgressCallback = Callable[[str, int], None]
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def ai_pdf_output_path(source: Path) -> Path:
    """Return the requested sibling name without changing the original stem."""
    source = Path(source)
    return source.with_name(f"{source.stem}(AI){source.suffix}")


def find_mineru_binary() -> Path | None:
    configured = os.getenv("QWEN_ASR_MINERU", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        RUNTIME_ROOT / "bin" / "mineru-kit",
        Path(shutil.which("mineru")) if shutil.which("mineru") else None,
        Path.home() / ".local" / "bin" / "mineru",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def validate_pdf_sources(raw_paths: Iterable[object]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        value = str(raw or "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError("PDF 路径必须是绝对路径")
        source = candidate.resolve()
        if source in seen:
            continue
        if source.suffix.lower() != ".pdf" or not source.is_file():
            raise ValueError(f"不是有效的 PDF 文件：{source.name}")
        if source.stem.endswith("(AI)"):
            raise ValueError(f"文件已经是 AI 友好版：{source.name}")
        target = ai_pdf_output_path(source)
        if target.exists():
            raise FileExistsError(f"同目录已存在：{target.name}")
        seen.add(source)
        paths.append(source)
    if not paths:
        raise ValueError("请至少选择一个 PDF 文件")
    if len(paths) > 20:
        raise ValueError("一次最多处理 20 个 PDF 文件")
    return paths


def _stop_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def _run_checked(command: list[str], log_path: Path, stop=None, timeout=None) -> str:
    check_stopped(stop)
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        started = time.monotonic()
        try:
            while result.poll() is None:
                check_stopped(stop)
                if timeout is not None and time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(.25)
        finally:
            _stop_process(result)
    check_stopped(stop)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if result.returncode:
        tail = "\n".join(output.replace("\r", "\n").splitlines()[-12:]).strip()
        raise RuntimeError(tail or f"命令执行失败（退出码 {result.returncode}）")
    return output


def _last_progress(text: str, pattern: str) -> tuple[int, int] | None:
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if not matches:
        return None
    match = matches[-1]
    return match.start(), int(match.group(1))


def _mineru_progress(text: str) -> tuple[str, int] | None:
    """Translate MinerU's carriage-return progress bars into one monotonic scale."""
    cleaned = ANSI_ESCAPE.sub("", text)
    windows = list(
        re.finditer(
            r"Hybrid processing window\s+(\d+)/(\d+):\s+pages\s+(\d+)-(\d+)/(\d+)",
            cleaned,
        )
    )
    window = int(windows[-1].group(1)) if windows else 1
    window_count = int(windows[-1].group(2)) if windows else 1
    if windows:
        page_start = int(windows[-1].group(3))
        page_end = int(windows[-1].group(4))
        total_pages = int(windows[-1].group(5))
        page_label = f"第 {page_start}–{page_end} 页"
    else:
        page_start = page_end = total_pages = 0
        page_label = "当前页段"
    window_label = (
        page_label
        if window_count == 1
        else f"{page_label}（处理段 {window}/{window_count}）"
    )
    segment = 66 / max(1, window_count)
    base = 8 + (window - 1) * segment

    def page_hint(percent: int) -> str:
        if not total_pages:
            return ""
        current = min(
            page_end,
            page_start + round((page_end - page_start) * percent / 100),
        )
        window_hint = f" · 处理段 {window}/{window_count}" if window_count > 1 else ""
        return f" · 约处理到原 PDF 第 {current}/{total_pages} 页{window_hint}"

    if windows:
        cleaned = cleaned[windows[-1].end():]
    candidates: list[tuple[int, str, int]] = []
    layout = _last_progress(cleaned, r"(?:^|[\r\n])Layout Predict:\s*(\d+)%")
    if layout:
        candidates.append(
            (
                layout[0],
                f"MinerU · 版面分析 {layout[1]}%{page_hint(layout[1])}",
                round(base + segment * 0.10 * layout[1] / 100),
            )
        )
    table = _last_progress(cleaned, r"(?:^|[\r\n])Table orientation[^:]*:\s*(\d+)%")
    if table:
        candidates.append(
            (
                table[0],
                f"MinerU · {window_label} · 表格结构 {table[1]}%",
                round(base + segment * 0.10),
            )
        )
    prediction = _last_progress(cleaned, r"(?:^|[\r\n])Predict:\s*(\d+)%")
    if prediction:
        candidates.append(
            (
                prediction[0],
                f"MinerU · 正文与公式识别 {prediction[1]}%{page_hint(prediction[1])}",
                round(base + segment * (0.10 + 0.90 * prediction[1] / 100)),
            )
        )

    later_phases = [
        (r"(?:^|[\r\n])MFR Predict:\s*(\d+)%", "公式复核", .15, .35),
        (r"(?:^|[\r\n])OCR-det:\s*(\d+)%", "文字区域检测", .35, .45),
        (r"(?:^|[\r\n])OCR-rec Predict:\s*(\d+)%", "文字识别", .45, .90),
        (r"(?:^|[\r\n])Processing pages:\s*(\d+)%", "整理逐页结果", .90, 1.0),
    ]
    for pattern, label, start, end in later_phases:
        parsed = _last_progress(cleaned, pattern)
        if parsed:
            candidates.append(
                (
                    parsed[0],
                    f"MinerU · {window_label} · {label} {parsed[1]}%",
                    round(base + segment * (start + (end - start) * parsed[1] / 100)),
                )
            )
    if not candidates:
        return None
    _, stage, percent = max(candidates, key=lambda item: item[0])
    return stage, max(1, min(99, percent))


def _run_mineru(
    command: list[str], log_path: Path, progress: ProgressCallback | None, stop=None
) -> str:
    check_stopped(stop)
    last_update: tuple[str, int] | None = None
    highest = 0
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while process.poll() is None:
                check_stopped(stop)
                time.sleep(0.75)
                text = log_path.read_text(encoding="utf-8", errors="replace")
                update = _mineru_progress(text)
                if update:
                    highest = max(highest, update[1])
                    update = (update[0], highest)
                if update and update != last_update:
                    last_update = update
                    if progress:
                        progress(*update)
        finally:
            _stop_process(process)
        returncode = process.returncode
    check_stopped(stop)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    update = _mineru_progress(output)
    if update:
        update = (update[0], max(highest, update[1]))
    if update and update != last_update and progress:
        progress(*update)
    if returncode:
        tail = "\n".join(output.replace("\r", "\n").splitlines()[-12:]).strip()
        raise RuntimeError(tail or f"MinerU 执行失败（退出码 {returncode}）")
    return output


def _single_output(root: Path, pattern: str, label: str) -> Path:
    matches = sorted(root.rglob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"MinerU 没有生成唯一的{label}（找到 {len(matches)} 个）")
    return matches[0]


def convert_pdf_to_ai(
    source: Path,
    *,
    builder_script: Path,
    python_executable: Path,
    progress: ProgressCallback | None = None,
    stop=None,
    settings=None,
    inference_metadata=None,
    inference_config=None,
    receipt_path=None,
    local_guard=None,
) -> Path:
    """Convert one PDF and atomically create its sibling ``(AI).pdf`` file."""
    source = Path(source).resolve()
    target = ai_pdf_output_path(source)
    if target.exists():
        raise FileExistsError(f"同目录已存在：{target.name}")
    settings = normalize_mineru_settings(settings) if settings is not None else mineru_settings.read()
    if not builder_script.is_file():
        raise RuntimeError("AI 友好 PDF 打包脚本不存在")

    def report(stage: str, percent: int) -> None:
        check_stopped(stop)
        if progress:
            progress(stage, percent)

    temporary_output: Path | None = None
    report("MinerU · 正在启动模型", 2)
    from .remote_operations import accepted_result_guard
    with accepted_result_guard(receipt_path), tempfile.TemporaryDirectory(prefix='qwenasr-pdf-ai-') as raw_work:
        work = Path(raw_work)
        mineru_output = work / "mineru"
        settings_path = work / "settings.json"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        # The SDK runs in its own environment, outside the ASR process. This
        # retains process-group cancellation and uses a single parse for exports.
        def compute_local():
            started = time.monotonic()
            mineru = find_mineru_binary()
            if mineru is None:
                raise RuntimeError("未找到 MinerU 4。请先运行 scripts/install_mineru4.command")
            info = runtime_info(mineru)
            if not info["compatible"]:
                raise RuntimeError(f"需要 MinerU 4.x，当前为 {info['version'] or '未知版本'}")
            first_line = mineru.read_text(encoding="utf-8").splitlines()[0]
            runtime_python = first_line.removeprefix("#!").strip()
            if not Path(runtime_python).is_file():
                raise RuntimeError("MinerU 启动脚本没有有效的 Python 解释器")
            _run_mineru(
                [runtime_python, str(builder_script.parent / "run_mineru4.py"),
                 str(source), "-o", str(mineru_output), "--settings", str(settings_path)],
                work / "mineru.log", report, stop,
            )
            return {"metrics": {"mineru_version": info.get("version", ""), "tier": settings["tier"],
                                "configured_vlm_engine": settings["vlm_engine"],
                                "parse_seconds_including_load": time.monotonic() - started}}
        def local_parse():
            with local_guard or contextlib.nullcontext():
                return compute_local()
        from .inference_providers import parse_document
        provider_metadata = parse_document(source, mineru_output, local_parse, report, stop, config=inference_config, receipt_path=receipt_path)
        if inference_metadata is not None:
            inference_metadata.update(provider_metadata)
        report("正在整理逐页转写与 LaTeX", 84)
        markdown = _single_output(mineru_output, "*.md", "Markdown")
        content_list = _single_output(
            mineru_output, "*_content_list.json", "逐页内容映射"
        )
        uncertain = "\ufffd" in markdown.read_text(encoding="utf-8")
        # Preserve all relative image references when the work directory is
        # removed: the complete result travels inside the generated PDF.
        bundle = mineru_output / "result.zip"
        if bundle.exists():
            with zipfile.ZipFile(bundle, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("provider.json", json.dumps(provider_metadata, ensure_ascii=False))
        else:
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(mineru_output.rglob("*")):
                    if path.is_file() and path != bundle:
                        archive.write(path, path.relative_to(mineru_output).as_posix())
                archive.writestr("provider.json", json.dumps(provider_metadata, ensure_ascii=False))

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{source.stem}-ai-", suffix=".pdf", dir=source.parent
        )
        os.close(descriptor)
        temporary_output = Path(temporary_name)
        temporary_output.unlink()
        try:
            report("正在写入隐藏文字层并嵌入转写附件", 91)
            _run_checked(
                [
                    str(python_executable),
                    str(builder_script),
                    "--source",
                    str(source),
                    "--markdown",
                    str(markdown),
                    "--content-list",
                    str(content_list),
                    "--output",
                    str(temporary_output),
                    "--title",
                    source.stem,
                    "--result-bundle",
                    str(bundle),
                ],
                work / "package.log",
                stop,
            )
            if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
                raise RuntimeError("AI 友好 PDF 没有正确生成")
            if target.exists():
                raise FileExistsError(f"同目录已存在：{target.name}")
            report("正在安全写入源文件目录", 98)
            os.replace(temporary_output, target)
            temporary_output = None
        finally:
            if temporary_output is not None:
                temporary_output.unlink(missing_ok=True)

    if progress:
        progress("转换完成 · 识别包含 � 标记，请对照原页核对" if uncertain else "转换完成", 100)
    return target
