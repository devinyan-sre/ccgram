"""Short-lived, one-shot task selection for ambiguous Telegram follow-ups."""

from __future__ import annotations

import time

from .config import config

Scope = tuple[int, int, int]
_focus: dict[Scope, tuple[str, float]] = {}


def select(chat_id: int, thread_id: int, user_id: int, task_id: str) -> None:
    """Select one task for the member's next unqualified message."""
    _focus[(chat_id, thread_id, user_id)] = (
        task_id.upper(),
        time.monotonic() + config.task_selection_ttl_seconds,
    )


def consume(chat_id: int, thread_id: int, user_id: int) -> str | None:
    """Consume a valid selection exactly once; expired selections fail closed."""
    selected = _focus.pop((chat_id, thread_id, user_id), None)
    if selected is None:
        return None
    task_id, expires_at = selected
    return task_id if expires_at >= time.monotonic() else None


def clear_for_testing() -> None:
    _focus.clear()


__all__ = ["clear_for_testing", "consume", "select"]
