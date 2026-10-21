"""Size complete note batches by input evidence and expected output, not counts."""
from __future__ import annotations

import json

from .ai import context_window_for, estimate_tokens, output_limit_for, provider_for_mode, provider_transport

PROTOCOL_RESERVE = 16_000
NOTE_OUTPUT_TOKENS = 32_768
BATCH_POLICY = 'complete-evidence-input-and-output-v2'


def output_budget(config, requested=NOTE_OUTPUT_TOKENS):
    provider = provider_for_mode(config.get('ai', {}), 'summary')
    # Context must still reserve room for AGY output beyond the target: the
    # CLI has no output-token flag and thinking also consumes output tokens.
    if provider_transport(provider) == 'agy_cli':
        return min(requested, NOTE_OUTPUT_TOKENS)
    return output_limit_for(config, 'summary', requested)


def request_budget(instructions, payload, config, requested=NOTE_OUTPUT_TOKENS, *, window=None):
    fields = {key: estimate_tokens(json.dumps(value, ensure_ascii=False)) for key, value in payload.items()}
    input_tokens = estimate_tokens(instructions) + estimate_tokens(json.dumps(payload, ensure_ascii=False))
    output = output_budget(config, requested)
    return {'input_tokens': input_tokens, 'output_reserve': output,
            'protocol_reserve': PROTOCOL_RESERVE, 'estimated_total': input_tokens + output + PROTOCOL_RESERVE,
            'context_window': context_window_for(config, 'summary') if window is None else window,
            'fields': fields, 'token_count_method': 'conservative_estimate'}


def require_budget(instructions, payload, config, requested=NOTE_OUTPUT_TOKENS, *, label='笔记请求', window=None):
    budget = request_budget(instructions, payload, config, requested, window=window)
    if budget['estimated_total'] > budget['context_window']:
        raise ValueError(f"{label}预计需要 {budget['estimated_total']:,} tokens"
                         f"（输入 {budget['input_tokens']:,}，输出预留 {budget['output_reserve']:,}，"
                         f"协议预留 {budget['protocol_reserve']:,}），超过已保存上下文预算 "
                         f"{budget['context_window']:,}；程序尚未发送请求，草稿与审查已保留，未截断内容")
    return budget


def pack_complete_items(items, payload_for, instructions, config, output_estimate, *, window=None):
    """Pack ordered whole items; reserve a prior proposal for bounded repairs.

    An oversized indivisible item fails explicitly. Never drop evidence or
    split an equation/source block to make an apparently successful request.
    """
    plans = []
    current = []
    # Respect the saved output budget when planning complete note batches.
    # This is a target, never an upstream hard limit or permission to truncate.
    target_output = int(min(output_budget(config),output_limit_for(config,'summary',NOTE_OUTPUT_TOKENS)) * .8)

    def fits(group):
        budget = request_budget(instructions, payload_for(group), config, window=window)
        return (budget['estimated_total'] + budget['output_reserve'] <= budget['context_window']
                and output_estimate(group) <= target_output)

    for item in items:
        candidate = [*current, item]
        if current and not fits(candidate):
            plans.append(current)
            current = []
        current.append(item)
        if len(current) == 1 and not fits(current):
            raise ValueError('单个完整知识或转写片段及其来源超过本批输入/输出预算；原资料已保留，未截断')
    if current:
        plans.append(current)
    return plans
