"""Quiet-hours (do-not-disturb) window for automated notifications.

During the configured window, automated outbound messages (queue worker
content, status/Ready notices) are sent with Telegram's
``disable_notification=True`` — they still arrive, just silently.
User-initiated command replies and interactive approval prompts are
unaffected (they bypass ``rate_limit_send_message``).

Configured via ``CCGRAM_QUIET_HOURS`` as ``"HH:MM-HH:MM"`` local time
(e.g. ``"23:00-08:00"``; wraps past midnight). Empty/unset disables.

Key functions: parse_spec(), is_quiet(), silent_kwargs().
"""

from __future__ import annotations

import datetime as _dt

import structlog

logger = structlog.get_logger()

_WINDOW_PARTS = 2


def parse_spec(spec: str) -> tuple[_dt.time, _dt.time] | None:
    """Parse ``"HH:MM-HH:MM"`` into (start, end); None when empty/invalid."""
    spec = spec.strip()
    if not spec:
        return None
    parts = spec.split("-")
    if len(parts) != _WINDOW_PARTS:
        logger.warning("Invalid CCGRAM_QUIET_HOURS %r (expected HH:MM-HH:MM)", spec)
        return None
    try:
        start = _dt.time.fromisoformat(parts[0].strip())
        end = _dt.time.fromisoformat(parts[1].strip())
    except ValueError:
        logger.warning("Invalid CCGRAM_QUIET_HOURS %r (expected HH:MM-HH:MM)", spec)
        return None
    if start == end:
        # Zero-length window — treat as disabled rather than always-on.
        return None
    return start, end


def is_quiet(window: tuple[_dt.time, _dt.time] | None, now: _dt.time) -> bool:
    """True when *now* falls inside the quiet window (handles midnight wrap)."""
    if window is None:
        return False
    start, end = window
    if start < end:
        return start <= now < end
    # Wraps midnight, e.g. 23:00-08:00.
    return now >= start or now < end


def silent_kwargs() -> dict[str, bool]:
    """Return ``{"disable_notification": True}`` during quiet hours, else {}.

    Reads the config singleton lazily so tests can monkeypatch it and the
    hook subprocess never imports config through this module.
    """
    # Lazy: config requires bot env vars; resolve only when the bot sends.
    from .config import config

    spec = getattr(config, "quiet_hours", "")
    window = parse_spec(spec)
    if window is None:
        return {}
    if is_quiet(window, _dt.datetime.now().time()):
        return {"disable_notification": True}
    return {}
