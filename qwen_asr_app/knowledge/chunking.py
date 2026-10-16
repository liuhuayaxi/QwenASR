"""Structural boundary-aware chunking for documents and classroom audio transcripts."""
from __future__ import annotations

import re
from typing import Any

PARSER_VERSION = "qwenasr-parser-v2"
CHUNKER_VERSION = "qwenasr-chunker-v3"
EMBEDDING_SPLIT_VERSION = "embedding-budget-v1"


def embedding_byte_budget(max_length: int) -> int:
    """Conservative Qwen byte-BPE bound, reserving room for special tokens.

    This does not load a tokenizer/model on the Mac. Every UTF-8 byte can be
    represented by at most one byte-BPE token; document inputs have no prompt.
    The worker still checks the actual token count without truncation.
    """
    return max(1, int(max_length) - 32)


def bounded_chunker_version(max_length: int) -> str:
    return f"{CHUNKER_VERSION}+{EMBEDDING_SPLIT_VERSION}-{int(max_length)}"

# Regex patterns for atomic structural blocks
_FORMULA_BLOCK_RE = re.compile(
    r"(\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\begin\{[a-zA-Z*]+\}[\s\S]*?\\end\{[a-zA-Z*]+\})",
    re.MULTILINE,
)
_INLINE_FORMULA_RE = re.compile(r"(\$[^\$\n]+?\$|\\\([^\n]+?\\\))")
_TABLE_BLOCK_RE = re.compile(r"((\n|^)(\|[^\n]+\|\n)+)", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Pattern to avoid splitting numbers:
# e.g., 3.14, 10,000, 1.2e-4, 2026-10-14, 99.8%
_NUMBER_RE = re.compile(r"(?<![\w.])[-+−]?(?:\d+(?:[.,]\d+)*|[.,]\d+)(?:e[+-]?\d+)?%?", re.IGNORECASE)


class Chunk:
    def __init__(
        self,
        text: str,
        heading: str | None = None,
        page: int | None = None,
        start: float | None = None,
        end: float | None = None,
        segment_ids: list[int] | None = None,
        chunk_index: int = 0,
    ) -> None:
        self.text = text.strip()
        self.heading = heading
        self.page = page
        self.start = start
        self.end = end
        self.segment_ids = list(segment_ids or [])
        self.chunk_index = chunk_index

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "heading": self.heading,
            "page": self.page,
            "start": self.start,
            "end": self.end,
            "segment_ids": self.segment_ids,
            "chunk_index": self.chunk_index,
        }


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _bounded_prose(text: str, budget: int) -> list[str]:
    """Lossless prose pieces, keeping inline math/numbers/words atomic.

    An individual atom larger than the budget remains intact. Ingest retains
    that full atom for lexical retrieval and reports the missing vector.
    """
    protected = sorted(
        [match.span() for pattern in (
            _INLINE_FORMULA_RE, _NUMBER_RE,
            re.compile(r"[A-Za-z_][A-Za-z_0-9./-]*"),
        ) for match in pattern.finditer(text)]
    )
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start
        size = 0
        while end < len(text):
            added = _utf8_size(text[end])
            if size + added > budget:
                break
            size += added
            end += 1
        if end == len(text):
            pieces.append(text[start:])
            break
        # Never cut a protected atom. Overlapping number/word spans are
        # deliberately resolved repeatedly before choosing the boundary.
        cut = max(start + 1, end)
        while True:
            overlaps = [(left, right) for left, right in protected
                        if left < cut < right]
            if not overlaps:
                break
            left = min(item[0] for item in overlaps)
            if left > start:
                cut = left
            else:
                cut = max(item[1] for item in overlaps)
        # Prefer a nearby sentence/space boundary without producing tiny
        # pieces; leave the boundary character in the preceding piece.
        if _utf8_size(text[start:cut]) <= budget:
            choices = [m.end() + start for m in re.finditer(
                r"[。！？!?;；\n\s]+", text[start:cut]
            )]
            choices = [value for value in choices if value >= start + (cut-start)//2]
            if choices:
                candidate = choices[-1]
                if not any(left < candidate < right for left, right in protected):
                    cut = candidate
        pieces.append(text[start:cut])
        start = cut
    return pieces


def _bounded_table(text: str, budget: int) -> list[str]:
    """Split only between complete rows, repeating the original table header."""
    lines = text.splitlines()
    header: list[str] = []
    if len(lines) >= 2:
        cells = lines[1].strip().strip("|").split("|")
        if cells and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells):
            header, lines = lines[:2], lines[2:]
    if not lines:
        return [text]
    result: list[str] = []
    rows: list[str] = []
    for line in lines:
        candidate = "\n".join([*header, *rows, line])
        if rows and _utf8_size(candidate) > budget:
            result.append("\n".join([*header, *rows]))
            rows = []
        rows.append(line)
        if _utf8_size("\n".join([*header, *rows])) > budget:
            # A single wide row/header must remain readable and complete.
            result.append("\n".join([*header, *rows]))
            rows = []
    if rows:
        result.append("\n".join([*header, *rows]))
    return result


def bound_chunks_for_embedding(chunks: list[Chunk], max_length: int) -> tuple[list[Chunk], bool]:
    """Fit structural chunks to a token contract without losing source text.

    Normal chunks retain their exact text. Long tables split by rows; prose
    splits without cutting inline math/numbers/words. Display formulas and
    individually oversized rows/atoms stay whole for explicit lexical-only
    handling. Every child keeps its parent's page/time/segment provenance.
    """
    budget = embedding_byte_budget(max_length)
    result: list[Chunk] = []
    changed = False
    for chunk in chunks:
        if _utf8_size(chunk.text) <= budget:
            parts = [chunk.text]
        else:
            parts = []
            for block in _split_into_blocks(chunk.text):
                content = block["content"]
                if _utf8_size(content) <= budget or block["type"] == "formula":
                    parts.append(content)
                elif block["type"] == "table":
                    parts.extend(_bounded_table(content, budget))
                else:
                    parts.extend(_bounded_prose(content, budget))
            changed = changed or parts != [chunk.text]
        for part in parts:
            if not part.strip():
                continue
            result.append(Chunk(
                text=part, heading=chunk.heading, page=chunk.page,
                start=chunk.start, end=chunk.end, segment_ids=chunk.segment_ids,
                chunk_index=len(result),
            ))
    return result, changed


def _split_into_blocks(text: str) -> list[dict[str, Any]]:
    """Tokenize text into structural blocks (paragraphs, formulas, tables, headings)."""
    blocks: list[dict[str, Any]] = []
    lines = text.splitlines(keepends=True)
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Check for heading
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            blocks.append({
                "type": "heading",
                "level": len(heading_match.group(1)),
                "title": heading_match.group(2).strip(),
                "content": stripped,
            })
            i += 1
            continue

        # Check for formula block start: $$ or \begin{...}
        if stripped.startswith("$$") or stripped.startswith("\\[") or stripped.startswith("\\begin{"):
            if ((stripped.startswith('$$') and stripped.count('$$') >= 2)
                    or (stripped.startswith('\\[') and '\\]' in stripped)
                    or (stripped.startswith('\\begin{') and '\\end{' in stripped)):
                blocks.append({'type':'formula', 'content':stripped})
                i += 1
                continue
            formula_lines = [line]
            closer = "$$" if stripped.startswith("$$") else ("\\]" if stripped.startswith("\\[") else "\\end{" + stripped.split("{")[1].split("}")[0] + "}")
            i += 1
            while i < n:
                f_line = lines[i]
                formula_lines.append(f_line)
                if closer in f_line:
                    i += 1
                    break
                i += 1
            blocks.append({
                "type": "formula",
                "content": "".join(formula_lines).strip(),
            })
            continue

        # Check for markdown table: line starts with '|' and contains '|'
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines = [line]
            i += 1
            while i < n and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table_lines.append(lines[i])
                i += 1
            blocks.append({
                "type": "table",
                "content": "".join(table_lines).strip(),
            })
            continue

        # Normal text paragraph: collect lines until empty line, heading, formula or table
        para_lines = [line]
        i += 1
        while i < n:
            next_line = lines[i]
            next_stripped = next_line.strip()
            if not next_stripped:
                i += 1
                break
            if _HEADING_RE.match(next_stripped):
                break
            if next_stripped.startswith("$$") or next_stripped.startswith("\\[") or next_stripped.startswith("\\begin{"):
                break
            if next_stripped.startswith("|") and next_stripped.endswith("|"):
                break
            para_lines.append(next_line)
            i += 1

        content = "".join(para_lines).strip()
        if content:
            blocks.append({
                "type": "paragraph",
                "content": content,
            })

    return blocks


def _split_long_paragraph(text: str, max_chars: int = 500) -> list[str]:
    """Split a long paragraph without breaking formulas, numbers, or words."""
    if len(text) <= max_chars:
        return [text]

    # Split on sentence boundaries: 。！？!? or \n
    sentence_delims = re.compile(r"([。！？!?\n]+)")
    parts = sentence_delims.split(text)

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for part in parts:
        if not part:
            continue
        # Check if adding part exceeds limit
        if current_len + len(part) > max_chars and current:
            # Check if previous ended with a number and part starts with decimal or digits
            # to prevent breaking number boundaries
            prev_tail = current[-1][-5:] if current else ""
            if re.search(r"\d+$", prev_tail) and re.match(r"^[.,\d]", part):
                current.append(part)
                current_len += len(part)
                continue

            chunks.append("".join(current).strip())
            current = [part]
            current_len = len(part)
        else:
            current.append(part)
            current_len += len(part)

    if current:
        chunks.append("".join(current).strip())

    return [c for c in chunks if c]


def chunk_markdown_text(
    text: str,
    page: int | None = None,
    max_chars: int = 600,
    min_chars: int = 80,
) -> list[Chunk]:
    """Chunk structured text (Markdown/Text/MinerU) respecting headings, tables, formulas, and numbers."""
    if not text or not text.strip():
        return []

    blocks = _split_into_blocks(text)
    chunks: list[Chunk] = []
    current_heading: str | None = None
    current_pieces: list[str] = []
    current_len = 0

    def flush_pieces():
        nonlocal current_pieces, current_len
        if not current_pieces:
            return
        combined = "\n\n".join(current_pieces).strip()
        if combined:
            chunks.append(
                Chunk(
                    text=combined,
                    heading=current_heading,
                    page=page,
                    chunk_index=len(chunks),
                )
            )
        current_pieces = []
        current_len = 0

    for block in blocks:
        b_type = block["type"]
        b_content = block["content"]

        if b_type == "heading":
            # Heading triggers a chunk boundary if we have accumulated content
            if current_len >= min_chars:
                flush_pieces()
            current_heading = block["title"]
            # We can also keep heading line in current_pieces if desired
            current_pieces.append(b_content)
            current_len += len(b_content)
            continue

        if b_type in ("formula", "table"):
            # If current pieces are non-empty and adding formula/table exceeds limit, flush first
            if current_pieces and (current_len + len(b_content) > max_chars):
                flush_pieces()
            current_pieces.append(b_content)
            current_len += len(b_content)
            # If the formula or table itself is very large, flush it as its own chunk
            if current_len >= max_chars:
                flush_pieces()
            continue

        # Regular paragraph
        if len(b_content) > max_chars:
            flush_pieces()
            sub_parts = _split_long_paragraph(b_content, max_chars=max_chars)
            for sub in sub_parts:
                chunks.append(
                    Chunk(
                        text=sub,
                        heading=current_heading,
                        page=page,
                        chunk_index=len(chunks),
                    )
                )
        else:
            if current_pieces and (current_len + len(b_content) > max_chars):
                flush_pieces()
            current_pieces.append(b_content)
            current_len += len(b_content)

    flush_pieces()
    return chunks


def chunk_pdf_document(pages_text: list[tuple[int, str]], max_chars: int = 600) -> list[Chunk]:
    """Chunk a PDF document page by page, preserving page metadata."""
    chunks: list[Chunk] = []
    for page_num, text in pages_text:
        page_chunks = chunk_markdown_text(text, page=page_num, max_chars=max_chars)
        for c in page_chunks:
            c.chunk_index = len(chunks)
            chunks.append(c)
    return chunks


def chunk_transcript_segments(
    segments: list[dict[str, Any]],
    max_chars: int = 400,
    max_seconds: float = 60.0,
    text_key: str = "text",
) -> list[Chunk]:
    """Chunk audio transcript segments into timestamped groups without breaking segments."""
    if not segments:
        return []

    # Sort segments by start time
    sorted_segs = sorted(
        segments,
        key=lambda s: (float(s.get("start", 0)), int(s.get("id", 0) or 0)),
    )

    chunks: list[Chunk] = []
    current_segs: list[dict[str, Any]] = []
    current_chars = 0

    def flush_segs():
        nonlocal current_segs, current_chars
        if not current_segs:
            return
        texts = []
        for s in current_segs:
            val = str(s.get(text_key) or s.get("corrected") or s.get("raw") or "").strip()
            if val:
                texts.append(val)
        combined_text = " ".join(texts).strip()
        if combined_text:
            start_t = float(current_segs[0].get("start", 0.0))
            end_t = float(current_segs[-1].get("end", start_t))
            seg_ids = list(dict.fromkeys(int(ident) for s in current_segs
                for ident in (s.get('segment_ids') or ([s['id']] if s.get('id') is not None else []))))
            chunks.append(
                Chunk(
                    text=combined_text,
                    start=start_t,
                    end=end_t,
                    segment_ids=seg_ids,
                    chunk_index=len(chunks),
                )
            )
        current_segs = []
        current_chars = 0

    for seg in sorted_segs:
        val = str(seg.get(text_key) or seg.get("corrected") or seg.get("raw") or "").strip()
        if not val:
            continue
        seg_len = len(val)

        # Check if duration or character budget would be exceeded
        duration_exceeded = False
        if current_segs:
            time_span = float(seg.get("end", 0.0)) - float(current_segs[0].get("start", 0.0))
            if time_span > max_seconds:
                duration_exceeded = True

        if current_segs and (current_chars + seg_len > max_chars or duration_exceeded):
            flush_segs()

        current_segs.append(seg)
        current_chars += seg_len

    flush_segs()
    return chunks
