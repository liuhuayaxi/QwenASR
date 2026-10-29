"""Validated, durable settings for the workbench's local MinerU 4 runtime."""
from __future__ import annotations

import json
from pathlib import Path

from .durable import atomic_json
from .paths import PRIVATE_ROOT

MINERU_VERSION = "4.0.10"
DEFAULT_MINERU_SETTINGS = {
    "tier": "standard", "ocr_mode": "auto", "image_analysis": True,
    "small_backend": "auto", "vlm_engine": "llama-cpp", "model_source": "modelscope",
    "processing_window_size": 0, "vlm_concurrency": 4,
    "virtual_vram_gb": 0,
}
CHOICES = {
    "tier": ("auto", "flash", "basic", "standard", "advanced"),
    "ocr_mode": ("auto", "txt", "ocr"),
    "small_backend": ("auto", "onnx", "torch"),
    "vlm_engine": ("auto", "llama-cpp", "vllm", "lmdeploy"),
    "model_source": ("auto", "modelscope", "huggingface", "local"),
}
LIMITS = {
    "processing_window_size": (0, 128), "vlm_concurrency": (1, 32),
    "virtual_vram_gb": (0, 128),
}


def normalize_mineru_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError("MinerU 配置必须是对象")
    unknown = set(raw) - set(DEFAULT_MINERU_SETTINGS)
    if unknown:
        raise ValueError("不支持这些 MinerU 参数：" + ", ".join(sorted(unknown)))
    result = {**DEFAULT_MINERU_SETTINGS, **raw}
    for key, values in CHOICES.items():
        if result[key] not in values:
            raise ValueError(f"MinerU 参数 {key} 无效")
    if not isinstance(result["image_analysis"], bool):
        raise ValueError("图像内容分析必须是 true 或 false")
    for key, (low, high) in LIMITS.items():
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"MinerU 参数 {key} 必须是 {low}–{high} 之间的整数")
    return result


class MinerUSettings:
    def __init__(self, path=None):
        self.path = path or PRIVATE_ROOT / "mineru-settings.json"

    def read(self):
        try:
            return normalize_mineru_settings(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return dict(DEFAULT_MINERU_SETTINGS)

    def save(self, raw):
        settings = normalize_mineru_settings(raw)
        atomic_json(self.path, settings)
        self.path.chmod(0o600)
        return settings


def runtime_info(binary: Path | None):
    if binary is None:
        return {"available": False, "version": "", "compatible": False, "path": ""}
    environment = binary.resolve().parent.parent
    versions = [p.name.removeprefix("mineru-").removesuffix(".dist-info")
                for p in environment.glob("lib/python*/site-packages/mineru-*.dist-info")]
    version = versions[0] if len(versions) == 1 else ""
    return {"available": True, "version": version, "compatible": version.startswith("4."),
            "path": str(binary)}


mineru_settings = MinerUSettings()
