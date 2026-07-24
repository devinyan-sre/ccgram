"""Pure decision kernel for window_tick — no I/O, no side effects.

All inputs flow in via ``TickContext``; output is a ``TickDecision``.
This module imports nothing that touches tmux, Telegram, or singletons,
so its functions are deterministic and trivially unit-testable.
"""

from __future__ import annotations

import time

from ....providers.base import StatusUpdate
from ....terminal_parser import status_emoji_prefix
from ..polling_types import (
    STARTUP_TIMEOUT,
    TYPING_MAX_QUIET,
    TickContext,
    TickDecision,
    is_shell_prompt,
)


def _typing_ok(ctx: TickContext) -> bool:
    """Whether to refresh the "typing…" action for an active/starting tick.

    Honest-typing gate: only while the agent is genuinely producing output —
    transcript activity within ``TYPING_MAX_QUIET``. A long think/spinner phase
    (the seconds counter ticking, no new transcript writes) stops refreshing so
    the indicator lapses instead of showing a perpetual, misleading "typing".
    ``CCGRAM_TYPING=0`` (``typing_enabled=False``) suppresses it entirely.
    """
    if not ctx.typing_enabled:
        return False
    if ctx.last_activity_ts is None:
        return False
    return (time.monotonic() - ctx.last_activity_ts) < TYPING_MAX_QUIET


def build_status_line(status: StatusUpdate | None) -> str | None:
    if not status or status.is_interactive:
        return None
    if "\n" in status.raw_text:
        return status.raw_text
    return f"{status_emoji_prefix(status.raw_text)} {status.raw_text}"


def decide_tick(ctx: TickContext) -> TickDecision:
    """Pure status/idle transition decision — no I/O, no side effects.

    All mutable state reads (``has_seen_status``, ``is_recently_active``,
    ``startup_time``) must be computed by the coordinator before building
    ``TickContext``. The ``is_recently_active`` flag is special: its
    computation in the coordinator may mark_seen_status as a side effect,
    so it must not be re-derived here.
    """
    if ctx.is_dead_window:
        return TickDecision(show_recovery=True)

    if ctx.resolved_status_text:
        return TickDecision(
            send_status=True,
            status_text=ctx.resolved_status_text,
            transition="active",
            send_typing=_typing_ok(ctx),
        )

    if ctx.is_recently_active:
        return TickDecision(transition="active", send_typing=_typing_ok(ctx))

    if ctx.is_shell_prompt:
        if ctx.supports_hook:
            return TickDecision(transition="done")
        return TickDecision(transition="idle")

    if ctx.has_seen_status:
        return TickDecision(transition="idle")

    startup_expired = (
        ctx.startup_time is not None
        and (time.monotonic() - ctx.startup_time) >= STARTUP_TIMEOUT
    )
    if startup_expired:
        return TickDecision(transition="idle")

    # Startup grace (bounded by STARTUP_TIMEOUT) — the user just launched/sent,
    # so show typing immediately even before the first transcript write; only
    # the CCGRAM_TYPING switch gates it here.
    return TickDecision(transition="starting", send_typing=ctx.typing_enabled)


__all__ = ["build_status_line", "decide_tick", "is_shell_prompt"]
