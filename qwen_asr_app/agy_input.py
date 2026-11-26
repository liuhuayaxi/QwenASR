"""Lossless wire encoding for characters AGY removes from USER_INPUT logs."""
from __future__ import annotations

import json
import unicodedata


JSON_REQUEST_HEADER = (
    '传输格式说明：下方 JSON 的 request 字段包含本次完整请求。'
    '先按 JSON 规则解码（包括 Unicode 转义），再执行其中的完整任务；'
    '原请求中的工具、文件访问限制和资料边界仍须全部遵守。\n'
)


def encode_prompt(prompt: str) -> str:
    """Preserve format/private-use characters without weakening exact matching.

    AGY 1.4.3 drops Cf/Co characters when persisting user messages. Native PDF
    text contains U+200B and font-specific private-use glyphs, so comparing its
    unsanitized prompt with that record falsely rejects a completed request.
    Send a JSON string whose affected characters are escaped instead. JSON
    decoding restores every original character; source files stay untouched.
    Literal USER_REQUEST tags must also remain data inside AGY's outer wrapper.
    Ordinary prompts keep their existing byte representation.
    """
    def needs_escape(character):
        return unicodedata.category(character) in {'Cf', 'Co'}

    if not any(needs_escape(c) for c in prompt) and not any(
            tag in prompt for tag in ('<USER_REQUEST>', '</USER_REQUEST>')):
        return prompt
    encoded = json.dumps({'request': prompt}, ensure_ascii=False)
    encoded = ''.join(json.dumps(c, ensure_ascii=True)[1:-1] if needs_escape(c) else c
                      for c in encoded)
    encoded = encoded.replace('<', '\\u003c').replace('>', '\\u003e')
    return JSON_REQUEST_HEADER + encoded
