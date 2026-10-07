"""Tiny dependency-free lazy import proxy for optional heavy runtimes."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


class LazyModule:
    def __init__(self, name: str) -> None:
        self._name = name
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            self._module = importlib.import_module(self._name)
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


def lazy_module(name: str) -> LazyModule:
    return LazyModule(name)
