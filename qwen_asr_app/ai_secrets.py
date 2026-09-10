"""User-approved local persistence for AI provider API keys."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROVIDER_MODES = ("sentence", "review", "summary", "correction", "global")


class AISecretStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / ".qwenasr" / "ai-provider-secrets.json")

    def _read(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            mode: str(raw.get(mode) or "").strip()
            for mode in PROVIDER_MODES
            if str(raw.get(mode) or "").strip()
        }

    def get(self, mode: str) -> str:
        return self._read().get(str(mode), "")

    def status(self) -> dict[str, bool]:
        saved = self._read()
        return {mode: bool(saved.get(mode)) for mode in PROVIDER_MODES}

    def update(self, values: Any) -> dict[str, bool]:
        if not isinstance(values, dict):
            raise ValueError("本机 AI 密钥格式无效")
        saved = self._read()
        for mode in PROVIDER_MODES:
            value = values.get(mode)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                if len(text) > 20_000:
                    raise ValueError("API Key 长度异常，未保存")
                saved[mode] = text
        if saved:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temp, 0o600)
            temp.replace(self.path)
        return self.status()

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


ai_secret_store = AISecretStore()
