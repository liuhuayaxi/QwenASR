"""Course terminology shared by local ASR and remote AI editing tasks."""
from __future__ import annotations

import re
from typing import Any, Iterable

from .context_budget import estimate_tokens, normalize_context_window


MAX_LIBRARY_TERMS = 500
MAX_TERM_CHARS = 120
MAX_ASR_TERMS = 100
MAX_ASR_GLOSSARY_TOKENS = 1_200
MAX_AI_GLOSSARY_TOKENS = 4_096


COURSE_TERM_SEEDS: dict[str, list[str]] = {
    "通信原理": [
        "信源", "信宿", "信道", "信息量", "自信息", "信源熵", "比特（bit）", "奈特（nat）",
        "香农公式", "信噪比（SNR）", "码元速率", "误码率", "带宽", "调制", "解调",
        "振幅键控（ASK）", "频移键控（FSK）", "相移键控（PSK）", "正交振幅调制（QAM）",
        "码间串扰（ISI）", "奈奎斯特准则", "脉冲成形", "升余弦滚降",
    ],
    "物理光学": [
        "惠更斯–菲涅耳原理", "波前", "波矢", "光程", "相位差", "相干", "干涉", "衍射",
        "偏振", "琼斯矩阵", "菲涅耳公式", "夫琅禾费衍射", "菲涅耳衍射", "傅里叶光学",
        "坡印廷矢量", "马吕斯定律", "布儒斯特角", "双折射",
    ],
    "光电子学": [
        "受激吸收", "自发辐射", "受激发射", "粒子数反转", "净受激发射速率", "光增益",
        "费米能级", "准费米能级", "直接带隙", "异质结", "载流子扩散", "辐射复合",
        "俄歇复合（Auger recombination）", "肖克利–里德–霍尔复合（SRH）", "发光二极管（LED）",
        "激光二极管（LD）", "增益系数", "注入效率", "内量子效率",
    ],
    "光电子器件双语": [
        "optical fiber（光纤）", "silica glass（二氧化硅玻璃）", "core（纤芯）",
        "cladding（包层）", "coating（涂覆层）", "refractive index（折射率）",
        "total internal reflection（全反射）", "single-mode fiber（SMF，单模光纤）",
        "multimode fiber（MMF，多模光纤）", "fundamental mode（基模）",
        "LP01 mode", "V-number（归一化频率）", "intermodal dispersion（模间色散）",
        "chromatic dispersion（色度色散）", "1.31 μm window", "1.55 μm window",
        "numerical aperture（NA，数值孔径）", "guided mode（导模）",
    ],
    "微型计算机原理与接口技术": [
        "8086", "8088", "中央处理器（CPU）", "算术逻辑单元（ALU）", "执行单元（EU）",
        "总线接口单元（BIU）", "段寄存器", "指令指针（IP）", "物理地址", "偏移地址",
        "堆栈", "寻址方式", "汇编语言", "中断向量", "输入输出（I/O）", "直接存储器访问（DMA）",
        "可编程并行接口 8255A", "可编程定时器 8253/8254", "地址总线", "数据总线", "控制总线",
    ],
}


def _term_text(value: Any) -> str:
    if isinstance(value, dict):
        preferred = str(value.get("term") or value.get("name") or "").strip()
        aliases = value.get("aliases")
        note = str(value.get("note") or "").strip()
        if isinstance(aliases, list):
            alias_text = "、".join(str(item).strip() for item in aliases if str(item).strip())
        else:
            alias_text = str(aliases or "").strip()
        parts = [preferred]
        if alias_text:
            parts.append(f"别名：{alias_text}")
        if note:
            parts.append(note)
        return " | ".join(part for part in parts if part)
    return str(value or "").strip()


def normalize_terminology(raw: Any, *, category: str = "", seed_if_missing: bool = False) -> list[str]:
    """Normalize editable one-term-per-line data without silently truncating it."""
    if raw is None:
        values: Iterable[Any] = COURSE_TERM_SEEDS.get(category, []) if seed_if_missing else []
    elif isinstance(raw, str):
        values = raw.splitlines()
    elif isinstance(raw, list):
        values = raw
    else:
        raise ValueError("课程术语库必须是字符串数组或每行一个术语的文本")

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", _term_text(value)).strip(" \t,，;；")
        if not text:
            continue
        if len(text) > MAX_TERM_CHARS:
            raise ValueError(f"术语“{text[:24]}…”超过 {MAX_TERM_CHARS} 个字符")
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    if len(result) > MAX_LIBRARY_TERMS:
        raise ValueError(f"每门课程最多保存 {MAX_LIBRARY_TERMS} 条术语")
    return result


def _fit_entries(entries: list[str], token_budget: int, max_entries: int) -> list[str]:
    selected: list[str] = []
    used = 0
    for entry in entries:
        cost = estimate_tokens(entry) + 2
        if selected and used + cost > token_budget:
            break
        if cost > token_budget:
            continue
        selected.append(entry)
        used += cost
        if len(selected) >= max_entries:
            break
    return selected


def asr_terminology_context(user_context: str, terminology: Any) -> str:
    """Build a bounded ASR system prompt; the whole library remains stored separately."""
    entries = _fit_entries(
        normalize_terminology(terminology), MAX_ASR_GLOSSARY_TOKENS, MAX_ASR_TERMS
    )
    parts = [str(user_context or "").strip()]
    if entries:
        parts.append("课程术语词库（优先识别为以下标准写法，不要把词库本身抄入转写）：\n" + "；".join(entries))
    return "\n\n".join(part for part in parts if part)


def asr_params_with_terminology(raw_params: Any, terminology: Any) -> dict[str, Any]:
    """Merge glossary entries into the runtime's supported hotwords field."""
    params = dict(raw_params or {}) if isinstance(raw_params, dict) else {}
    existing = params.get("hotwords") if isinstance(params.get("hotwords"), list) else []
    combined = normalize_terminology(existing) + normalize_terminology(terminology)
    selected: list[str] = []
    seen: set[str] = set()
    for entry in combined:
        key = entry.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(entry)
        if len(selected) >= MAX_ASR_TERMS:
            break
    params["hotwords"] = selected
    return params


def ai_terminology_context(
    terminology: Any,
    *,
    context_window: Any,
    source_text: str = "",
) -> str:
    """Select a bounded glossary for an AI request, preferring terms seen nearby."""
    entries = normalize_terminology(terminology)
    if not entries:
        return ""
    haystack = str(source_text or "").casefold()
    ranked = sorted(
        enumerate(entries),
        key=lambda item: (0 if any(piece.casefold() in haystack for piece in re.split(r"[（(｜|、,/ ]", item[1]) if len(piece) >= 2) else 1, item[0]),
    )
    window = normalize_context_window(context_window)
    budget = min(MAX_AI_GLOSSARY_TOKENS, max(64, window // 20))
    chosen = _fit_entries([entry for _, entry in ranked], budget, MAX_LIBRARY_TERMS)
    return "课程术语库（只用于校正标准写法，不得据此添加原文没有的内容）：\n" + "\n".join(
        f"- {entry}" for entry in chosen
    )


def terminology_stats(terminology: Any, *, context_window: Any = 32_768) -> dict[str, int]:
    entries = normalize_terminology(terminology)
    asr_entries = _fit_entries(entries, MAX_ASR_GLOSSARY_TOKENS, MAX_ASR_TERMS)
    ai_text = ai_terminology_context(entries, context_window=context_window)
    return {
        "stored_terms": len(entries),
        "stored_tokens": estimate_tokens("\n".join(entries)),
        "asr_terms": len(asr_entries),
        "asr_tokens": estimate_tokens("\n".join(asr_entries)),
        "ai_terms": max(0, ai_text.count("\n- ")),
        "ai_tokens": estimate_tokens(ai_text),
        "max_stored_terms": MAX_LIBRARY_TERMS,
        "max_asr_terms": MAX_ASR_TERMS,
    }
