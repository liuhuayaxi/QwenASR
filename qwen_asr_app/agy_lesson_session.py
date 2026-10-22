"""One AGY conversation per lesson for review-note generation.

Before: every model call of a note (knowledge plan, writing, each LaTeX batch,
abstract) started a fresh ``agy`` process and a fresh conversation, and each
re-sent the full transcript, source pages, learning scope and coverage. For a
typical lesson that was ~10 cold starts and the same ~90k-token context
re-read (as 20+ file chunks) four or five times.

Now ``review_notes.generate`` opens a LessonSession. Each text request made
while it is active continues the same AGY conversation (``--conversation``)
and replaces every top-level INPUT field (and the instructions) that is
byte-identical to one already sent in this conversation with a short
reference, so the transcript, materials and forced questions are loaded once.

Guarantees kept:
- Each turn is still verified from the durable AGY transcript; earlier turns
  must be exactly the prompts this session sent (``prior_prompts``).
- A turn that fails while continuing is retried once as a fresh conversation
  with the complete payload (the old behaviour), and the session restarts.
- The session restarts with a full payload when its accumulated estimate
  would exceed the token budget (default 60% of the context window, or the
  ``agy_session_token_budget`` setting).
- State is persisted in the lesson's task folder, so resuming after a restart
  continues the same conversation when it is still valid.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .context_budget import estimate_tokens

current: ContextVar["LessonSession | None"] = ContextVar("agy_lesson_session", default=None)
DEFAULT_BUDGET_SHARE = 0.6
VERSION = 1


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class LessonSession:
    def __init__(self, folder: Path, *, budget_tokens: int | None = None, context_window: int = 1_048_576):
        self.folder = Path(folder) / "agy-lesson-session"
        self.budget = int(budget_tokens or context_window * DEFAULT_BUDGET_SHARE)
        self.lock = asyncio.Lock()
        self.session_id = ""
        self.prompts: list[str] = []
        self.sent: dict[str, int] = {}  # field digest -> turn number
        self.tokens = 0
        self.stats = {"continued_turns": 0, "fresh_turns": 0, "fallbacks": 0, "restarts": 0,
                      "deduplicated_fields": 0, "deduplicated_tokens": 0}
        self._load()

    # -- persistence ---------------------------------------------------
    def _state_path(self) -> Path:
        return self.folder / "state.json"

    def _load(self) -> None:
        try:
            state = json.loads(self._state_path().read_text())
            prompts = [(self.folder / f"turn-{i:03d}.txt").read_text() for i in range(1, state["turns"] + 1)]
        except (OSError, ValueError, KeyError):
            return
        if state.get("version") != VERSION or _sha(prompts) != state.get("prompts_sha256"):
            return
        self.session_id, self.prompts = state["session_id"], prompts
        self.sent = {k: int(v) for k, v in state.get("sent", {}).items()}
        self.tokens = int(state.get("tokens", 0))
        self.stats.update(state.get("stats", {}))

    def _save(self) -> None:
        from .durable import atomic_json, atomic_bytes
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        for index, prompt in enumerate(self.prompts, 1):
            path = self.folder / f"turn-{index:03d}.txt"
            if not path.exists():
                atomic_bytes(path, prompt.encode())
        atomic_json(self._state_path(), {"version": VERSION, "session_id": self.session_id, "turns": len(self.prompts),
                                         "prompts_sha256": _sha(self.prompts), "sent": self.sent,
                                         "tokens": self.tokens, "stats": self.stats})

    def reset(self, reason: str) -> None:
        if self.session_id:
            self.stats["restarts"] += 1
        self.session_id, self.prompts, self.sent, self.tokens = "", [], {}, 0
        for path in self.folder.glob("turn-*.txt"):
            path.unlink(missing_ok=True)
        self.stats["last_restart_reason"] = reason
        self._save()

    # -- request shaping -------------------------------------------------
    def plan(self, instructions: str, payload: dict[str, Any]):
        """Return (instructions, payload, conversation_id) for the next turn."""
        full = estimate_tokens(instructions) + estimate_tokens(json.dumps(payload, ensure_ascii=False))
        if not self.session_id:
            return instructions, payload, ""
        if self.tokens + full > self.budget:
            self.reset("budget")
            return instructions, payload, ""
        turn = len(self.prompts) + 1
        shaped: dict[str, Any] = {}
        saved = 0
        for key, value in payload.items():
            earlier = self.sent.get(_sha([key, value]))
            if earlier and estimate_tokens(json.dumps(value, ensure_ascii=False)) > 200:
                shaped[key] = f"〔与本会话第 {earlier} 轮 INPUT.{key} 完全相同，内容见上文，未重复发送〕"
                saved += estimate_tokens(json.dumps(value, ensure_ascii=False))
                self.stats["deduplicated_fields"] += 1
            else:
                shaped[key] = value
        text = instructions
        earlier = self.sent.get(_sha(["__instructions__", instructions]))
        if earlier and estimate_tokens(instructions) > 200:
            text = f"〔SYSTEM INSTRUCTIONS 与本会话第 {earlier} 轮完全相同，按上文执行；只处理本轮 INPUT〕"
            saved += estimate_tokens(instructions)
        self.stats["deduplicated_tokens"] += saved
        del turn
        return text, shaped, self.session_id

    def record(self, *, session_id: str, prompt: str, instructions: str, payload: dict[str, Any],
               response: str, continued: bool) -> None:
        if not continued:
            # A fresh conversation: only this turn is in it.
            self.prompts, self.sent, self.tokens = [], {}, 0
        self.session_id = session_id
        self.prompts.append(prompt)
        turn = len(self.prompts)
        for key, value in payload.items():
            self.sent.setdefault(_sha([key, value]), turn)
        self.sent.setdefault(_sha(["__instructions__", instructions]), turn)
        self.tokens += estimate_tokens(prompt) + estimate_tokens(response)
        self.stats["continued_turns" if continued else "fresh_turns"] += 1
        self._save()


def open_session(folder, config) -> LessonSession | None:
    """A LessonSession for agy_cli summary providers unless disabled."""
    from .ai import provider_for_mode
    provider = provider_for_mode(config.get("ai", {}), "summary")
    if provider.get("transport") != "agy_cli" or config.get("agy_lesson_session") is False:
        return None
    window = int(provider.get("context_window") or 1_048_576)
    budget = config.get("agy_session_token_budget")
    return LessonSession(folder, budget_tokens=int(budget) if isinstance(budget, int) and budget > 0 else None,
                         context_window=window)
