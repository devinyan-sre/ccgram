"""Correlate provider output with the Telegram message that requested it."""

from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(frozen=True, slots=True)
class RequestContext:
    user_id: int
    chat_id: int
    thread_id: int
    message_id: int
    created_at: float


_active: dict[str, RequestContext] = {}


def record_request(
    window_id: str,
    *,
    user_id: int,
    chat_id: int,
    thread_id: int,
    message_id: int,
    preserve_existing: bool = False,
) -> None:
    """Record the latest human request accepted by one isolated window."""
    existing = _active.get(window_id)
    if (
        preserve_existing
        and existing is not None
        and existing.user_id == user_id
        and existing.thread_id == thread_id
    ):
        return
    _active[window_id] = RequestContext(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        created_at=time.time(),
    )


def reply_message_id(window_id: str, *, user_id: int, thread_id: int) -> int | None:
    """Return a safe Telegram reply target only when the route still matches."""
    request = _active.get(window_id)
    if request is None:
        # Lazy: keep the correlation helper usable in small unit tests and
        # to avoid making the durable journal part of the cold import cycle.
        from .inbound_store import inbound_store

        recovered = inbound_store.active_for_window(window_id)
        if recovered is not None:
            request = RequestContext(
                user_id=recovered.user_id,
                chat_id=recovered.chat_id,
                thread_id=recovered.thread_id,
                message_id=recovered.message_id,
                created_at=recovered.created_at,
            )
            _active[window_id] = request
    if request is None:
        return None
    if request.user_id != user_id or request.thread_id != thread_id:
        return None
    return request.message_id


def clear_window(window_id: str) -> None:
    _active.pop(window_id, None)


def reset_for_testing() -> None:
    _active.clear()


__all__ = [
    "RequestContext",
    "clear_window",
    "record_request",
    "reply_message_id",
    "reset_for_testing",
]
