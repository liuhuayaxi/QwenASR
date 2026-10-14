"""Verification, numeric checks, entity validation, and display reconstruction for factual claims."""
from __future__ import annotations

import difflib
import re
from typing import Any

from ..ai import parse_json_object

CLAIM_SECTIONS = ('classroom', 'material', 'ai_explanation', 'check')
NEGATION_WORDS = ('不', '未', '无', '非', '禁止', '没有', '并非', '不得', '不能')
UNIT_PATTERN = re.compile(
    r'(?i)\b(?:db|nm|μm|um|km|m|cm|mm|ghz|mhz|khz|hz|mw|w|v|rad|s|ms|ps)\b|[°度]',
)


def extract_numbers(text: str) -> list[str]:
    """Extract numeric tokens from text for exact claim-to-quote verification."""
    matches = re.findall(r'(?<![A-Za-z_])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?:\s*(?:度|°))?', str(text))
    tokens = []
    for token in matches:
        cleaned = token.replace('度', '°').strip()
        if cleaned:
            tokens.append(cleaned)
    return tokens


def extract_units(text: str) -> list[str]:
    """Extract measurement units for consistency checking."""
    return [u.lower().replace('度', '°') for u in UNIT_PATTERN.findall(str(text or ''))]


def extract_technical_terms(text: str) -> set[str]:
    """Extract technical terms, Latin identifiers and acronyms (2+ letters)."""
    return {
        t.lower() for t in re.findall(r'\b[A-Za-z][A-Za-z0-9_-]{1,}\b', str(text or ''))
        if t.lower() not in {'is', 'are', 'the', 'a', 'an', 'to', 'in', 'on', 'of', 'and', 'or', 'for', 'it', 'by', 'as', 'at'}
    }


def normalize_quote(text: str) -> str:
    """Normalize whitespace and punctuation for quote containment checks."""
    cleaned = re.sub(r'[\s，。！？、；：""\'\'“”‘’()（）]+', ' ', str(text or '')).strip()
    return cleaned


def is_faithful_quote(statement: str, quote: str) -> bool:
    """Check if statement is a faithful/direct quote rather than a free paraphrase."""
    s_norm = normalize_quote(statement)
    q_norm = normalize_quote(quote)
    if not s_norm or not q_norm:
        return False
    if s_norm in q_norm or q_norm in s_norm:
        return True
    ratio = difflib.SequenceMatcher(None, s_norm, q_norm).ratio()
    return ratio >= 0.82


def check_entailment_and_consistency(statement: str, quote: str) -> tuple[bool, str]:
    """Conservative check: numbers, units, negation, and technical entities.

    Quote containment alone does not prove statement entailment. Free paraphrases
    that flip negation, alter units, or introduce unsupported entities are rejected.
    """
    stmt_norm = normalize_quote(statement)
    quote_norm = normalize_quote(quote)

    # 1. Numeric check: statement numbers must appear in the quote
    stmt_nums = extract_numbers(stmt_norm)
    quote_nums = set(extract_numbers(quote_norm))
    unsupported_nums = [n for n in stmt_nums if n not in quote_nums]
    if unsupported_nums:
        return False, f'数值核查失败：陈述包含未经原话证实的数值 {unsupported_nums}'

    # 2. Negation check: statement and quote must not contradict in polarity
    stmt_neg = any(w in stmt_norm for w in NEGATION_WORDS)
    quote_neg = any(w in quote_norm for w in NEGATION_WORDS)
    if stmt_neg != quote_neg:
        return False, '极性核查失败：陈述与引文在肯定/否定极性上不一致'

    # 3. Units check: units in statement must be grounded in quote
    stmt_units = extract_units(stmt_norm)
    quote_units = set(extract_units(quote_norm))
    unsupported_units = [u for u in stmt_units if u not in quote_units]
    if unsupported_units:
        return False, f'单位核查失败：陈述包含原话中不存在的量纲/单位 {unsupported_units}'

    # 4. Technical terms & acronyms check: technical identifiers must appear in quote
    stmt_terms = extract_technical_terms(stmt_norm)
    quote_terms = extract_technical_terms(quote_norm)
    unsupported_terms = [t for t in stmt_terms if t not in quote_terms]
    if unsupported_terms:
        return False, f'实体核查失败：陈述包含未经原话证实的专业术语或实体 {unsupported_terms}'

    return True, ''


def reconstruct_enhanced_content(
    claims: dict[str, Any],
    raw_text: str = '',
    empty_evidence: bool = False,
) -> str:
    """Reconstruct formatted note display strictly from verified claim sections.

    Unchecked enhanced_content from models is never displayed as verified text.
    Faithful quotes are displayed as verified evidence; paraphrases are marked interpretation pending.
    """
    if empty_evidence:
        return (
            f"> **速记草稿**：{raw_text}\n\n"
            f"> ⚠️ *当前课堂转写与课程资料库中未检索到相关证据，原速记已完整保留，请补充时间戳或关键词后重试。*"
        )

    classroom_claims = claims.get('classroom') or []
    material_claims = claims.get('material') or []
    ai_explanations = claims.get('ai_explanation') or []
    pending_interpretations = claims.get('interpretation_pending') or []
    check_notes = claims.get('check') or {}

    sections: list[str] = []

    if classroom_claims:
        lines = ['### 课堂讲解要点（已核对课堂转写）']
        for c in classroom_claims:
            time_str = f" [{c.get('time')}]" if c.get('time') else ""
            lines.append(f"- **[{c.get('citation_id', '课堂')}]** {c.get('quote') or c.get('statement')}{time_str}")
            if c.get('statement') and c.get('quote') and c.get('statement') != c.get('quote'):
                lines.append(f"  - 讲授要点：{c.get('statement')}")
        sections.append('\n'.join(lines))

    if material_claims:
        lines = ['### 教材与课程资料依据（已核对资料库）']
        for c in material_claims:
            page_str = f"第 {c.get('page')} 页" if c.get('page') else ""
            head_str = f"（{c.get('heading')}）" if c.get('heading') else ""
            meta = f" - 《{c.get('title', '资料')}》{page_str}{head_str}" if (c.get('title') or page_str or head_str) else ""
            lines.append(f"- **[{c.get('citation_id', '教材')}]** {c.get('quote') or c.get('statement')}{meta}")
            if c.get('statement') and c.get('quote') and c.get('statement') != c.get('quote'):
                lines.append(f"  - 资料要点：{c.get('statement')}")
        sections.append('\n'.join(lines))

    if pending_interpretations:
        lines = ['### 语义解读（待复核）', '> [!NOTE]', '> 以下为自由转述概括，未经原话逐字证实，请对照引文：']
        for item in pending_interpretations:
            lines.append(f"- **[{item.get('citation_id')}]** {item.get('statement')}")
            lines.append(f"  > 原文依据：“{item.get('quote')}”")
        sections.append('\n'.join(lines))

    if ai_explanations:
        lines = [
            '### AI 知识补充与推导（待学生复核）',
            '> [!NOTE]',
            '> 以下内容为 AI 推理补充，未经课堂或教材原文逐字核验，请结合实际讲授复核：',
        ]
        for item in ai_explanations:
            lines.append(f"- {item.get('statement', '')}")
        sections.append('\n'.join(lines))

    if not classroom_claims and not material_claims and not pending_interpretations:
        sections.insert(
            0,
            f"> **速记原文**：{raw_text}\n\n"
            f"> ⚠️ *本次整理未能通过课堂或资料库的事实核查，未生成已核实正文。*"
        )

    if check_notes and isinstance(check_notes, dict) and any(check_notes.values()):
        notes_text = check_notes.get('notes') or check_notes.get('consistency_check') or ''
        if notes_text:
            sections.append(f"**模型自检（待核对）**：{notes_text}")

    return '\n\n'.join(sections).strip() + '\n'


def verify_claims(
    raw_response: dict[str, Any] | str,
    classroom_evidence: list[dict[str, Any]],
    material_evidence: list[dict[str, Any]],
    raw_text: str = '',
) -> dict[str, Any]:
    """Strictly validate factual claims against allowed classroom and material evidence."""
    if isinstance(raw_response, str):
        parsed = parse_json_object(raw_response)
    elif isinstance(raw_response, dict):
        parsed = raw_response
    else:
        raise ValueError('AI 响应格式无效，需为 JSON 对象')

    raw_claims = parsed.get('claims')
    if not isinstance(raw_claims, dict):
        raise ValueError('AI 接口输出缺少 claims 架构')

    cls_map: dict[str, dict[str, Any]] = {}
    for item in classroom_evidence:
        cid = str(item.get('citation_id') or '')
        if cid:
            cls_map[cid] = item

    mat_map: dict[str, dict[str, Any]] = {}
    for item in material_evidence:
        cid = str(item.get('citation_id') or '')
        if cid:
            mat_map[cid] = item

    verified_claims: list[dict[str, Any]] = []
    rejected_claims: list[dict[str, Any]] = []
    ai_explanations: list[dict[str, Any]] = []
    interpretation_pending: list[dict[str, Any]] = []

    # 1. Verify Classroom Claims
    raw_classroom = raw_claims.get('classroom') or []
    if not isinstance(raw_classroom, list):
        raw_classroom = []
    for claim in raw_classroom:
        if not isinstance(claim, dict):
            continue
        statement = str(claim.get('statement') or claim.get('claim') or '').strip()
        citation_id = str(claim.get('citation_id') or '').strip()
        quote = str(claim.get('quote') or '').strip()

        if not statement:
            continue

        if citation_id not in cls_map:
            rejected_claims.append({
                'type': 'classroom',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': f'引用的依据编号 {citation_id or "空"} 不在允许的课堂转写范围内',
            })
            continue

        ev = cls_map[citation_id]
        if ev.get('type') != 'classroom' or ev.get('source') not in {'raw_asr', 'refined'}:
            rejected_claims.append({
                'type': 'classroom',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': '引用的课堂依据类型无效或来自未定稿的预览，预览不可作为已核实课堂依据',
            })
            continue

        ev_norm = normalize_quote(ev.get('text', ''))
        quote_norm = normalize_quote(quote)
        if not quote_norm or quote_norm not in ev_norm:
            rejected_claims.append({
                'type': 'classroom',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': f'引文未在课堂转写原文中精确包含（引文: "{quote[:30]}..."）',
            })
            continue

        consistent, reason = check_entailment_and_consistency(statement, quote)
        if not consistent:
            rejected_claims.append({
                'type': 'classroom',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': reason,
            })
            continue

        # Check faithful quote vs free paraphrase
        if is_faithful_quote(statement, quote):
            verified_claims.append({
                'type': 'classroom',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'time': ev.get('time', ''),
                'source': ev.get('source', ''),
                'source_id': ev.get('source_id', ''),
                'chunk_id': ev.get('chunk_id', ''),
                'content_hash': ev.get('content_hash', ''),
                'revision': ev.get('revision', ''),
                'open_url': ev.get('open_url', ''),
                'verified': True,
                'rejected': False,
            })
        else:
            # Display faithful quote as verified evidence; do NOT label free paraphrase verified
            verified_claims.append({
                'type': 'classroom',
                'statement': quote,
                'citation_id': citation_id,
                'quote': quote,
                'time': ev.get('time', ''),
                'source': ev.get('source', ''),
                'source_id': ev.get('source_id', ''),
                'chunk_id': ev.get('chunk_id', ''),
                'content_hash': ev.get('content_hash', ''),
                'revision': ev.get('revision', ''),
                'open_url': ev.get('open_url', ''),
                'verified': True,
                'rejected': False,
            })
            interpretation_pending.append({
                'type': 'classroom',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'time': ev.get('time', ''),
                'verified': False,
                'status': 'interpretation_pending',
                'notice': '语义转述待核验：该表述属于自由概括，请核对原文依据',
            })

    # 2. Verify Material Claims
    raw_material = raw_claims.get('material') or []
    if not isinstance(raw_material, list):
        raw_material = []
    for claim in raw_material:
        if not isinstance(claim, dict):
            continue
        statement = str(claim.get('statement') or claim.get('claim') or '').strip()
        citation_id = str(claim.get('citation_id') or '').strip()
        quote = str(claim.get('quote') or '').strip()

        if not statement:
            continue

        if citation_id not in mat_map:
            rejected_claims.append({
                'type': 'material',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': f'引用的教材依据编号 {citation_id or "空"} 不在允许的资料范围内',
            })
            continue

        ev = mat_map[citation_id]
        if ev.get('stale'):
            rejected_claims.append({
                'type': 'material',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': '引用的教材资料版本已过期失效',
            })
            continue

        ev_norm = normalize_quote(ev.get('text', ''))
        quote_norm = normalize_quote(quote)
        if not quote_norm or quote_norm not in ev_norm:
            rejected_claims.append({
                'type': 'material',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': '引文未在教材依据中精确包含',
            })
            continue

        consistent, reason = check_entailment_and_consistency(statement, quote)
        if not consistent:
            rejected_claims.append({
                'type': 'material',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'verified': False,
                'rejected': True,
                'reason': reason,
            })
            continue

        if is_faithful_quote(statement, quote):
            verified_claims.append({
                'type': 'material',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'title': ev.get('title', ''),
                'page': ev.get('page'),
                'heading': ev.get('heading', ''),
                'source_id': ev.get('source_id', ''),
                'revision': ev.get('revision', ''),
                'verified': True,
                'rejected': False,
            })
        else:
            # Display faithful quote as verified evidence; do NOT label free paraphrase verified
            verified_claims.append({
                'type': 'material',
                'statement': quote,
                'citation_id': citation_id,
                'quote': quote,
                'title': ev.get('title', ''),
                'page': ev.get('page'),
                'heading': ev.get('heading', ''),
                'source_id': ev.get('source_id', ''),
                'revision': ev.get('revision', ''),
                'verified': True,
                'rejected': False,
            })
            interpretation_pending.append({
                'type': 'material',
                'statement': statement,
                'citation_id': citation_id,
                'quote': quote,
                'title': ev.get('title', ''),
                'page': ev.get('page'),
                'verified': False,
                'status': 'interpretation_pending',
                'notice': '语义转述待核验：该表述属于资料自由概括，请核对原文依据',
            })

    # 3. AI Explanation Section
    raw_ai = raw_claims.get('ai_explanation') or []
    if not isinstance(raw_ai, list):
        raw_ai = []
    for item in raw_ai:
        if isinstance(item, str):
            stmt = item.strip()
        elif isinstance(item, dict):
            stmt = str(item.get('statement') or item.get('content') or item.get('text') or '').strip()
        else:
            continue
        if stmt:
            ai_explanations.append({
                'type': 'ai_explanation',
                'statement': stmt,
                'needs_review': True,
                'verified': False,
                'notice': '此部分为 AI 知识补充或推理推导，未经课堂或教材原话逐字验证，请学生复核',
            })

    # 4. Check Section
    check = raw_claims.get('check') or {}
    if not isinstance(check, dict):
        check = {'notes': str(check)}

    validation = {
        'valid': len(rejected_claims) == 0,
        'verified_claims_count': len(verified_claims),
        'rejected_claims_count': len(rejected_claims),
        'interpretation_pending_count': len(interpretation_pending),
        'needs_review_count': len(ai_explanations),
        'verified_claims': verified_claims,
        'rejected_claims': rejected_claims,
        'interpretation_pending': interpretation_pending,
        'needs_review': ai_explanations,
        'check': check,
    }

    claims_result = {
        'classroom': [c for c in verified_claims if c['type'] == 'classroom'],
        'material': [c for c in verified_claims if c['type'] == 'material'],
        'interpretation_pending': interpretation_pending,
        'ai_explanation': ai_explanations,
        'check': check,
    }

    reconstructed_content = reconstruct_enhanced_content(claims_result, raw_text=raw_text)
    if rejected_claims:
        reconstructed_content = f'> {len(rejected_claims)} 条 AI 陈述未通过来源核验，已从正文中移除。请以明确引用的原文为准。\n\n' + reconstructed_content

    return {
        'enhanced_content': reconstructed_content,
        'claims': claims_result,
        'validation': validation,
    }
