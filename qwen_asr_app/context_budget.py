from __future__ import annotations

import math
import re
from typing import Any, Iterable

# A conservative common denominator for the long-context models configured by
# this app.  Individual providers can still lower or raise this per function.
DEFAULT_CONTEXT_WINDOW = 131_072
MIN_CONTEXT_WINDOW = 2_048
MAX_CONTEXT_WINDOW = 2_000_000

_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3040-\u30ff\uac00-\ud7af]"
)


def normalize_context_window(value: Any) -> int:
    try:
        tokens = int(value or DEFAULT_CONTEXT_WINDOW)
    except (TypeError, ValueError) as exc:
        raise ValueError("Context Window 必须是整数") from exc
    if not MIN_CONTEXT_WINDOW <= tokens <= MAX_CONTEXT_WINDOW:
        raise ValueError(
            f"Context Window 必须在 {MIN_CONTEXT_WINDOW}–{MAX_CONTEXT_WINDOW} tokens 之间"
        )
    return tokens


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate for Chinese/English text."""
    value = str(text or "")
    if not value:
        return 0
    cjk = len(_CJK_RE.findall(value))
    non_cjk = len(value) - cjk
    return max(1, cjk + math.ceil(non_cjk / 3))


def truncate_tail_to_tokens(text: str, max_tokens: int) -> str:
    """Keep the newest suffix under an estimated token budget."""
    value = str(text or "")
    if max_tokens <= 0:
        return ""
    if estimate_tokens(value) <= max_tokens:
        return value
    low, high = 0, len(value)
    while low < high:
        mid = (low + high) // 2
        if estimate_tokens(value[mid:]) <= max_tokens:
            high = mid
        else:
            low = mid + 1
    return value[low:]


def context_safe_source_budget(context_window: Any, fixed_overhead: int = 1_200) -> int:
    """Budget source text so a roughly same-length rewrite can also fit."""
    window = normalize_context_window(context_window)
    return max(384, (window - fixed_overhead) // 2)


def fit_previous_context(
    transcript: str,
    previous_context: str,
    instructions: str,
    context_window: Any,
    output_tokens: int,
    payload_overhead: int = 320,
) -> tuple[str, int]:
    """Reserve output space and trim only old context, never current transcript."""
    window = normalize_context_window(context_window)
    desired_output = max(128, min(int(output_tokens), 32_768, window // 2))
    fixed_input = estimate_tokens(instructions) + estimate_tokens(transcript) + payload_overhead
    available_for_history = window - desired_output - fixed_input
    if available_for_history < 0:
        raise ValueError(
            "当前文本超过所填写的 Context Window；请提高窗口值或缩短单次处理片段"
        )
    return truncate_tail_to_tokens(previous_context, available_for_history), desired_output


def chunk_text_blocks(
    blocks: Iterable[str], context_window: Any, *, max_source_tokens: int | None = None,
    fixed_overhead: int = 1_200, use_full_window: bool = False,
) -> list[str]:
    """Join complete blocks without exceeding the safe rewrite input budget."""
    budget = (max(128, normalize_context_window(context_window) - fixed_overhead)
              if use_full_window else context_safe_source_budget(context_window, fixed_overhead))
    if max_source_tokens is not None:
        budget = min(budget, max(128, max_source_tokens))
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for raw_block in blocks:
        block = str(raw_block or "").strip()
        if not block:
            continue
        block_tokens = estimate_tokens(block)
        if block_tokens > budget:
            # A single unusually long ASR block is split only as a last resort.
            if current:
                chunks.append("\n\n".join(current))
                current, current_tokens = [], 0
            cursor = 0
            while cursor < len(block):
                low, high = cursor + 1, len(block)
                best = low
                while low <= high:
                    mid = (low + high) // 2
                    if estimate_tokens(block[cursor:mid]) <= budget:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                chunks.append(block[cursor:best])
                cursor = best
            continue
        if current and current_tokens + block_tokens + 2 > budget:
            chunks.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(block)
        current_tokens += block_tokens + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks
