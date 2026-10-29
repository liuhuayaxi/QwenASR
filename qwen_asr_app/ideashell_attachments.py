"""Build IdeaShell attachment arguments from the live MCP tool schema."""
from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any


# IdeaShell currently accepts one audio attachment of up to 25 MiB per call.
# Keep this limit here so preview and push enforce the same contract.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
AUDIO_FIELDS = ("audios", "audio_attachments", "audioAttachments", "audio_files")


def _resolve(schema: dict[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node: Any = schema
        try:
            for part in ref[2:].split("/"):
                node = node[part]
        except (KeyError, TypeError):
            return {}
        value = node if isinstance(node, dict) else {}
    for key in ("anyOf", "oneOf"):
        alternatives = value.get(key)
        if isinstance(alternatives, list):
            candidate = next(
                (item for item in alternatives if isinstance(item, dict) and item.get("type") != "null"),
                {},
            )
            return _resolve(schema, candidate)
    return value


def audio_attachment_arguments(
    schema: dict[str, Any], filename: str, data: bytes
) -> dict[str, Any]:
    return audio_attachment_arguments_many(schema, [{"name": filename, "data": data}])


def audio_attachment_arguments_many(
    schema: dict[str, Any], attachments: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return the audio argument accepted by a live note tool schema.

    The server has changed attachment support over time, so inspect the schema
    instead of blindly sending a field that an older grant would reject.
    """
    return attachment_arguments(schema, attachments, AUDIO_FIELDS, "录音", 5)


def attachment_arguments(schema, attachments, fields, label, default_limit):
    """Encode attachments using the currently advertised parameter shape."""
    if not isinstance(schema, dict):
        raise ValueError(f"闪念贝壳未返回{label}附件参数，请重新连接后重试")
    properties = schema.get("properties") or {}
    for field in fields:
        definition = _resolve(schema, properties.get(field))
        if not definition:
            continue
        item_schema: dict[str, Any]
        if definition.get("type") == "array":
            if len(attachments) > int(definition.get("maxItems") or default_limit):
                continue
            item_schema = _resolve(schema, definition.get("items"))
        elif definition.get("type") == "object":
            if len(attachments) > 1:
                continue
            item_schema = definition
        else:
            continue
        item_properties = item_schema.get("properties") or {}
        name_key = next(
            (key for key in ("name", "filename", "file_name", "fileName") if key in item_properties),
            None,
        )
        data_key = next(
            (
                key
                for key in (
                    "data",
                    "base64",
                    "base64_data",
                    "base64_content",
                    "content_base64",
                    "file_base64",
                    "content",
                )
                if key in item_properties
            ),
            None,
        )
        if not name_key or not data_key:
            continue
        items = []
        for attachment in attachments:
            filename = str(attachment.get("name") or "录音.aac")
            data = attachment.get("data") or b""
            item = {name_key: filename, data_key: base64.b64encode(data).decode("ascii")}
            for key in ("mime_type", "mimeType", "content_type"):
                if key in item_properties:
                    item[key] = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            required = set(item_schema.get("required") or [])
            if not required.issubset(item):
                items = []
                break
            items.append(item)
        if items:
            return {field: items if definition.get("type") == "array" else items[0]}
    raise ValueError(f"当前闪念贝壳接口未提供可用的{label}附件参数，请重新连接后重试")


def image_attachment_batches(schema, attachments, max_bytes=None):
    """Split images into calls of at most maxItems (and max_bytes raw, if set)."""
    if not attachments:
        return []
    definition = _resolve(schema, (schema.get('properties') or {}).get('images'))
    limit = min(9, int(definition.get('maxItems') or 9))
    if limit < 1:
        raise ValueError('当前闪念贝壳接口不接受图片附件')
    groups, current, size = [], [], 0
    for attachment in attachments:
        length = len(attachment.get('data') or b'')
        if current and (len(current) >= limit or (max_bytes and size + length > max_bytes)):
            groups.append(current)
            current, size = [], 0
        current.append(attachment)
        size += length
    groups.append(current)
    return [attachment_arguments(schema, group, ('images',), '图片', 9) for group in groups]


def document_attachment_arguments(schema, attachments):
    return attachment_arguments(schema, attachments, ('documents',), '文档', 5)


def audio_file_metadata(path: Path, *, filename: str | None = None) -> dict[str, Any]:
    """Read and validate one local audio file for an IdeaShell upload."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取录音文件：{exc}") from exc
    if not data:
        raise ValueError("录音文件为空，无法推送到闪念贝壳")
    if len(data) > MAX_AUDIO_BYTES:
        limit_mb = MAX_AUDIO_BYTES / (1024 * 1024)
        actual_mb = len(data) / (1024 * 1024)
        raise ValueError(f"录音文件为 {actual_mb:.1f} MB，超过闪念贝壳单个录音附件 {limit_mb:.0f} MB 限制")
    name = filename or path.name
    return {
        "name": name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
    }
