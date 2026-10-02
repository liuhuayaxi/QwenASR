"""Install a narrow, reversible UTF-8 repair in the selected MinerU environment.

This only edits source on disk; it never imports MLX, loads a model, or starts a
conversion. Atomic replacement avoids modifying uv's shared hard-linked cache.
"""
from __future__ import annotations

import ast
import hashlib
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .durable import atomic_json
from .paths import PRIVATE_ROOT

PATCH_VERSION = "incremental-utf8-v2"
MARKER = f'_qwenasr_utf8_patch = "{PATCH_VERSION}"'
REPLACEMENT = '''    _qwenasr_utf8_patch = "incremental-utf8-v2"

    def reset(self):
        import codecs
        self.offset = 0
        self._unflushed = ""
        self.text = ""
        self.tokens = []
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._utf8_warned = False

    def _append_utf8(self, text):
        if "\\ufffd" in text and not self._utf8_warned:
            import sys
            print(
                "QWENASR_MINERU_UTF8_REPLACEMENT: 模型输出含无效字节，已用 U+FFFD 标记，请核对原页。",
                file=sys.stderr, flush=True,
            )
            self._utf8_warned = True
        if not self.text and self.trim_space and text.startswith(" "):
            text = text[1:]
        self.text += text

    def add_token(self, token, skip_special_token_ids=()):
        token = int(token)
        if token in skip_special_token_ids:
            return
        self.tokens.append(token)
        raw = bytes(self._byte_decoder[c] for c in self.tokenmap[token])
        # Incomplete Unicode characters remain buffered across token boundaries.
        self._append_utf8(self._utf8_decoder.decode(raw, final=False))

    def finalize(self):
        self._append_utf8(self._utf8_decoder.decode(b"", final=True))

    @property
    def last_segment(self):
        # Replacement markers represent invalid output, not an incomplete byte
        # sequence; include them in the final text so they can be reviewed.
        segment = self.text[self.offset:]
        self.offset = len(self.text)
        return segment
'''


def _replace_file(path: Path, data: bytes, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def ensure_mineru_utf8_patch(binary: Path) -> dict:
    binary = Path(binary).expanduser().resolve()
    environment = binary.parent.parent
    sources = list(environment.glob("lib/python*/site-packages/mlx_vlm/tokenizer_utils.py"))
    if not sources:
        # Non-MLX installations do not use the affected code path.
        return {"status": "not_applicable"}
    if len(sources) != 1:
        raise RuntimeError("MinerU 环境中发现多个 MLX 解码器，无法确定目标，已停止自动修改")
    path = sources[0]
    original = path.read_bytes()
    source = original.decode("utf-8")
    if MARKER in source:
        return {"status": "installed", "version": PATCH_VERSION, "path": str(path)}
    tree = ast.parse(source, filename=str(path))
    target = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BPEStreamingDetokenizer"), None)
    if target is None:
        return {"status": "not_applicable"}
    methods = {node.name: node for node in target.body if isinstance(node, ast.FunctionDef)}
    required = {"reset", "add_token", "finalize", "make_byte_decoder"}
    if not required <= methods.keys():
        raise RuntimeError("MLX 解码器结构已变化，原文件保留，需要更新兼容修复")
    add_source = ast.get_source_segment(source, methods["add_token"])
    if '.decode("utf-8")' not in (add_source or ""):
        return {"status": "upstream_changed", "path": str(path)}
    # Restrict the edit to the three known, adjacent streaming methods.
    streaming = [node.name for node in target.body if isinstance(node, ast.FunctionDef)
                 and methods["reset"].lineno <= node.lineno <= methods["finalize"].lineno]
    if streaming != ["reset", "add_token", "finalize"]:
        raise RuntimeError("MLX 解码方法布局不匹配，原文件保留，需要更新兼容修复")
    lines = source.splitlines(keepends=True)
    updated = "".join(lines[:methods["reset"].lineno - 1]) + REPLACEMENT + "".join(lines[methods["finalize"].end_lineno:])
    # Parse the replacement as source only; do not execute the dependency.
    ast.parse(updated, filename=str(path))
    digest = hashlib.sha256(original).hexdigest()
    backup = PRIVATE_ROOT / "compat-backups" / "mineru-mlx-utf8" / digest
    backup.mkdir(parents=True, exist_ok=True)
    original_backup = backup / "tokenizer_utils.py"
    if original_backup.exists():
        if hashlib.sha256(original_backup.read_bytes()).hexdigest() != digest:
            raise RuntimeError("已有解码器备份校验不一致，未修改环境")
    else:
        _replace_file(original_backup, original, 0o600)
    metadata = {"target": str(path), "backup": str(original_backup), "original_sha256": digest,
                "patched_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
                "version": PATCH_VERSION, "applied_at": datetime.now(timezone.utc).isoformat()}
    atomic_json(backup / "patch.json", metadata)
    if path.read_bytes() != original:
        raise RuntimeError("解码器在修补时发生了变化，未覆盖文件")
    _replace_file(path, updated.encode("utf-8"), stat.S_IMODE(path.stat().st_mode))
    for bytecode in (path.parent / "__pycache__").glob("tokenizer_utils.*.pyc"):
        bytecode.unlink(missing_ok=True)
    if path.read_bytes() != updated.encode("utf-8"):
        raise RuntimeError("解码器补丁落盘校验失败，请使用备份恢复")
    return {"status": "patched", **metadata}
