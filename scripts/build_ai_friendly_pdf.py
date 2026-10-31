#!/usr/bin/env python3
"""Package a raster PDF and MinerU output into one AI-readable PDF.

The original pages are preserved visually.  Each original page receives an
invisible, extractable transcript containing page-scoped text and LaTeX.
The MinerU Markdown and content-list JSON are also embedded as attachments.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pymupdf as fitz


SKIP_TYPES = {"footer", "page_number", "header"}
DEFAULT_FONT = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")


def normalize(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(normalize(item) for item in value if normalize(item))
    text = str(value).replace("\x00", " ").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def item_text(item: dict) -> str:
    kind = item.get("type", "unknown")
    if kind in SKIP_TYPES:
        return ""

    if kind == "equation":
        value = normalize(item.get("text") or item.get("content"))
        return f"[FORMULA_LATEX]\n{value}" if value else ""

    if kind == "table":
        value = normalize(
            item.get("table_body") or item.get("text") or item.get("content")
        )
        return f"[TABLE_HTML]\n{value}" if value else "[VISUAL_ONLY: table]"

    if kind in {"image", "chart"}:
        captions = normalize(
            item.get("image_caption")
            or item.get("chart_caption")
            or item.get("content")
        )
        marker = f"[VISUAL_ONLY: {kind}; inspect the original page image]"
        if item.get('analysis_source') == 'model_generated_unverified':
            marker += '\n[MODEL_IMAGE_ANALYSIS_UNVERIFIED: estimates are not source measurements]'
        return f"{marker}\n[CAPTION] {captions}" if captions else marker

    value = normalize(item.get("text") or item.get("content"))
    return f"[TEXT]\n{value}" if value else ""


def build_page_transcripts(content_list: list[dict], page_count: int) -> list[str]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for item in content_list:
        page_idx = item.get("page_idx")
        if not isinstance(page_idx, int) or not 0 <= page_idx < page_count:
            continue
        text = item_text(item)
        if text:
            grouped[page_idx].append(text)

    transcripts = []
    for page_idx in range(page_count):
        body = "\n\n".join(grouped.get(page_idx, [])) or '[NO_TEXT_EXTRACTED: page present; inspect original image]'
        transcripts.append(
            f"[[AI_TRANSCRIPT_PAGE:{page_idx + 1}]]\n"
            f"SOURCE_PDF_PAGE: {page_idx + 1}\n"
            "The following is MinerU OCR/LaTeX. The original page image is "
            "authoritative when the transcript is ambiguous.\n\n"
            f"{body}\n"
            f"[[END_AI_TRANSCRIPT_PAGE:{page_idx + 1}]]"
        )
    return transcripts


def insert_cover_text(page, rect, text, *, font_file, fontsize, color, lineheight=1.3):
    """Fit visible cover text and fail explicitly instead of silently omitting it."""
    size = fontsize
    while size >= 6:
        shape = page.new_shape()
        remaining = shape.insert_textbox(rect, text, fontname="AIUnicode",
            fontfile=str(font_file), fontsize=size, color=color, lineheight=lineheight)
        if remaining >= 0:
            shape.commit()
            return
        size -= 0.5
    raise ValueError("AI PDF cover text does not fit its layout")


def add_cover(
    doc: fitz.Document, title: str, source_name: str, stats: dict, font_file: Path
) -> None:
    width, height = stats["page_size"]
    # The explanatory cover needs its own readable canvas. Source pages keep
    # their exact dimensions; a small slide must not clip the cover or drop its
    # instructions when a textbox returns a negative remaining height.
    width, height = max(width, 595), max(height, 842)
    page = doc.new_page(width=width, height=height)
    blue = (0.12, 0.32, 0.68)
    dark = (0.10, 0.13, 0.19)
    muted = (0.35, 0.39, 0.46)

    page.draw_rect(fitz.Rect(0, 0, width, 14), color=blue, fill=blue)
    insert_cover_text(page,
        fitz.Rect(56, 48, width - 56, 90),
        "AI 友好型 PDF / AI-readable PDF",
        font_file=font_file,
        fontsize=25,
        color=dark,
    )
    insert_cover_text(page,
        fitz.Rect(56, 100, width - 56, 165),
        title,
        font_file=font_file,
        fontsize=17,
        color=blue,
    )

    body = (
        "本文件保留原始课件页面，并在每个原始页面中加入不可见但可提取的机器转写层。\n"
        "AI 阅读说明：\n"
        "1. 查找 [[AI_TRANSCRIPT_PAGE:n]] 可获得原 PDF 第 n 页的逐页转写。\n"
        "2. [FORMULA_LATEX] 后的内容是 MinerU 识别的 LaTeX 公式。\n"
        "3. [VISUAL_ONLY] 表示该区域应查看原始页面图像，不能只依赖转写。\n"
        "4. OCR/公式识别可能存在错误；有歧义时，以原始课件页面图像为准。\n\n"
        "The PDF contains page-scoped hidden OCR and LaTeX. Prefer the transcript for "
        "search and text processing, but verify ambiguous equations and diagrams against "
        "the original page image."
    )
    insert_cover_text(page,
        fitz.Rect(56, 180, width - 56, height - 180),
        body,
        font_file=font_file,
        fontsize=11.5,
        lineheight=1.55,
        color=dark,
    )

    info = (
        f"原始文件：{source_name}\n"
        f"原始页数：{stats['page_count']}\n"
        f"识别内容块：{stats['item_count']}\n"
        f"独立公式块：{stats['equation_count']}\n"
        f"生成日期：{date.today().isoformat()}\n"
        "说明页为新增第 1 页，因此原 PDF 第 n 页对应本文件第 n+1 页。"
    )
    page.draw_rect(
        fitz.Rect(56, height - 155, width - 56, height - 34),
        color=(0.84, 0.87, 0.92),
        fill=(0.96, 0.97, 0.99),
        width=0.8,
    )
    insert_cover_text(page,
        fitz.Rect(69, height - 143, width - 69, height - 41),
        info,
        font_file=font_file,
        fontsize=7.2,
        lineheight=1.15,
        color=muted,
    )


def add_hidden_transcript(page: fitz.Page, transcript: str, font_file: Path) -> float:
    rect = fitz.Rect(8, 8, page.rect.width - 8, page.rect.height - 8)
    for fontsize in (2.2, 1.8, 1.4, 1.0, 0.7, 0.5):
        result = page.insert_textbox(
            rect,
            transcript,
            fontname="AIUnicode",
            fontfile=str(font_file),
            fontsize=fontsize,
            lineheight=1.05,
            color=(0, 0, 0),
            render_mode=3,
            overlay=True,
        )
        if result >= 0:
            return fontsize
    raise RuntimeError("Transcript is too large to fit invisibly on one page")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--content-list", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--result-bundle", type=Path)
    parser.add_argument("--middle-json", type=Path)
    args = parser.parse_args()

    if not args.font.is_file():
        raise FileNotFoundError(f"Unicode font not found: {args.font}")

    source = fitz.open(args.source)
    content_list = json.loads(args.content_list.read_text(encoding="utf-8"))
    if not isinstance(content_list, list):
        raise TypeError("MinerU content list must be a JSON array")

    page_count = len(source)
    indices = [item.get('page_idx') for item in content_list]
    if any(not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < page_count for index in indices):
        raise ValueError('Invalid or out-of-range content page index')
    observed_pages = set(indices)
    expected_pages = set(range(page_count))
    middle_path = args.middle_json or args.content_list.parent/'middle_json.json'
    if middle_path.is_file():
        middle = json.loads(middle_path.read_text(encoding='utf-8'))
        pages = middle.get('pages', middle.get('pdf_info', []))
        directory = [page.get('page_idx') for page in pages if isinstance(page, dict)]
        if (middle.get('is_full_document') is False or len(directory) != page_count
                or any(isinstance(i, bool) or not isinstance(i, int) for i in directory)
                or set(directory) != expected_pages):
            raise ValueError('Materialized Middle JSON page directory is incomplete or invalid')
    elif observed_pages != expected_pages:
        raise ValueError(f'Page mapping mismatch; missing={sorted(expected_pages-observed_pages)}, extra=[]; Middle JSON required for empty pages')

    transcripts = build_page_transcripts(content_list, page_count)
    stats = {
        "page_count": page_count,
        "item_count": len(content_list),
        "equation_count": sum(1 for item in content_list if item.get("type") == "equation"),
        "page_size": (source[0].rect.width, source[0].rect.height),
    }

    # Build and subset the Unicode transcript layer before importing the
    # original pages.  Subsetting after importing can rewrite fonts used by
    # the source PDF and alter its appearance.
    transcript_layer = fitz.open()
    add_cover(transcript_layer, args.title, args.source.name, stats, args.font)
    used_sizes = []
    for page_idx, transcript in enumerate(transcripts):
        source_rect = source[page_idx].rect
        layer_page = transcript_layer.new_page(
            width=source_rect.width, height=source_rect.height
        )
        used_sizes.append(
            add_hidden_transcript(layer_page, transcript, args.font)
        )
    transcript_layer.subset_fonts(fallback=True)

    output = fitz.open()
    output.insert_pdf(transcript_layer, from_page=0, to_page=0)
    output.insert_pdf(source)
    for page_idx in range(page_count):
        target_page = output[page_idx + 1]
        target_page.show_pdf_page(
            target_page.rect,
            transcript_layer,
            page_idx + 1,
            overlay=True,
        )

    transcript_name = "ai-page-transcript.txt"
    transcript_blob = "\n\n".join(transcripts).encode("utf-8")
    output.embfile_add(
        transcript_name,
        transcript_blob,
        filename=transcript_name,
        ufilename=transcript_name,
        desc="Page-scoped OCR and LaTeX transcript",
    )
    output.embfile_add(
        "mineru-transcript.md",
        args.markdown.read_bytes(),
        filename="mineru-transcript.md",
        ufilename="mineru-transcript.md",
        desc="MinerU Markdown transcript",
    )
    output.embfile_add(
        "mineru-content-list.json",
        args.content_list.read_bytes(),
        filename="mineru-content-list.json",
        ufilename="mineru-content-list.json",
        desc="MinerU page and bounding-box content map",
    )
    if args.result_bundle:
        output.embfile_add("mineru-result.zip", args.result_bundle.read_bytes(),
                           filename="mineru-result.zip", ufilename="mineru-result.zip",
                           desc="Complete MinerU result, image assets and provider metadata")

    metadata = source.metadata.copy()
    metadata.update(
        {
            "title": f"{args.title}（AI友好版）",
            "subject": "Original slides with page-scoped hidden MinerU OCR and LaTeX",
            "keywords": "AI-readable, MinerU, OCR, LaTeX, searchable PDF",
            "creator": "MinerU + PyMuPDF AI-friendly PDF packager",
            "producer": "PyMuPDF",
        }
    )
    output.set_metadata(metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.save(args.output, garbage=4, deflate=True, clean=True)
    output.close()
    transcript_layer.close()
    source.close()

    print(
        json.dumps(
            {
                "output": str(args.output),
                "original_pages": page_count,
                "output_pages": page_count + 1,
                "items": len(content_list),
                "equations": stats["equation_count"],
                "attachments": 3 + int(bool(args.result_bundle)),
                "hidden_font_size_min": min(used_sizes),
                "hidden_font_size_max": max(used_sizes),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
