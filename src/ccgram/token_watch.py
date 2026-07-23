"""Live token/context watch — warns when a session approaches limits.

Fed raw transcript entries by TranscriptReader as they stream in; assistant
entries carry per-turn ``message.usage`` blocks. Two independent warnings:

- **Context**: the latest turn's ``input + cache_read + cache_creation``
  approximates the current context size. Crossing
  ``CCGRAM_CONTEXT_WARN`` percent of ``CCGRAM_CONTEXT_LIMIT`` emits a
  "consider /compact" warning once; it re-arms after the context shrinks
  (e.g. post-compaction), so each fill-up warns once.
- **Cumulative**: total tokens across the session crossing
  ``CCGRAM_TOKEN_WARN`` warns once per session (0 = disabled, default).

Sessions adopted mid-way undercount cumulative totals (tracking starts at
the current file end); the context warning self-corrects on the next turn.
Non-Claude providers have no ``message.usage`` and naturally no-op.

Key singleton: token_watch (mirrors claude_task_state's module pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from .i18n import t

logger = structlog.get_logger()

# Re-arm the context warning once usage drops below this fraction of the
# warn threshold (a compaction typically cuts context far below it).
_REARM_FRACTION = 0.7


@dataclass
class _SessionTokenState:
    total_tokens: int = 0
    context_tokens: int = 0
    total_warned: bool = False
    context_warned: bool = False


def _usage_of(entry: dict) -> dict | None:
    """Return the usage block of an assistant transcript entry, or None."""
    if entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    return usage if isinstance(usage, dict) else None


# Counts at or above this render as thousands ("163k").
_K = 1000


def _fmt_k(n: int) -> str:
    """Format a token count as thousands (e.g. 163k)."""
    return f"{round(n / _K)}k" if n >= _K else str(n)


@dataclass
class TokenWatch:
    """Per-session token accounting with threshold warnings."""

    _sessions: dict[str, _SessionTokenState] = field(default_factory=dict)

    def record_entries(self, session_id: str, entries: list[dict]) -> list[str]:
        """Fold new transcript entries in; return any warning messages."""
        # Lazy: config singleton resolved at call time so tests can swap it
        from .config import config

        context_warn_pct = config.context_warn_pct
        context_limit = config.context_limit_tokens
        total_warn = config.token_warn_total
        if context_warn_pct <= 0 and total_warn <= 0:
            return []

        state = self._sessions.setdefault(session_id, _SessionTokenState())
        warnings: list[str] = []

        for entry in entries:
            usage = _usage_of(entry)
            if usage is None:
                continue
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)

            state.total_tokens += (
                input_tokens + output_tokens + cache_read + cache_write
            )

            # Sidechain (subagent) turns have their own, smaller context —
            # they must not reset or re-arm the main context tracking.
            if not entry.get("isSidechain"):
                state.context_tokens = input_tokens + cache_read + cache_write

        threshold = context_limit * context_warn_pct // 100
        if context_warn_pct > 0:
            if (
                state.context_warned
                and state.context_tokens < threshold * _REARM_FRACTION
            ):
                state.context_warned = False
            if not state.context_warned and state.context_tokens >= threshold:
                state.context_warned = True
                warnings.append(
                    t(
                        "⚠️ Context is {pct}% full ({used} / {limit} tokens) — "
                        "consider /compact or a fresh session."
                    ).format(
                        pct=round(state.context_tokens * 100 / context_limit),
                        used=_fmt_k(state.context_tokens),
                        limit=_fmt_k(context_limit),
                    )
                )

        if (
            total_warn > 0
            and not state.total_warned
            and state.total_tokens >= total_warn
        ):
            state.total_warned = True
            warnings.append(
                t(
                    "⚠️ This session has consumed {total} tokens "
                    "(warning threshold: {threshold})."
                ).format(total=_fmt_k(state.total_tokens), threshold=_fmt_k(total_warn))
            )

        return warnings

    def clear_session(self, session_id: str) -> None:
        """Drop per-session state (session ended or replaced)."""
        self._sessions.pop(session_id, None)

    def reset(self) -> None:
        """Clear all state (testing)."""
        self._sessions.clear()


token_watch = TokenWatch()
