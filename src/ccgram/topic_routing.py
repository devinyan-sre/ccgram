"""Narrow topic-routing port for handler read access."""

from __future__ import annotations

from .thread_router import thread_router


def resolve_window(user_id: int, thread_id: int) -> str | None:
    """Return the window bound to a user's topic, if any."""
    return thread_router.get_window_for_thread(user_id, thread_id)


def resolve_chat(user_id: int, thread_id: int) -> int:
    """Return the Telegram chat id that owns a user's topic."""
    return thread_router.resolve_chat_id(user_id, thread_id)


__all__ = ["resolve_chat", "resolve_window"]
