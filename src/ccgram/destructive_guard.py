"""Circuit breaker: suspend automated destruction during infrastructure events.

One window dying is a user closing a session. Six dying inside a second is a
tmux server restart — an infrastructure event, not six independent intentions.
Every automated cleanup path treated those two cases identically, which is how
2026-07-25 played out: the server restarted at 09:48:15, six windows died at
once, and the autoclose sweep destroyed four topics seventeen minutes later.

This module makes that distinction. Window deaths are recorded in a sliding
window; once ``mass_death_threshold`` of them land inside
``mass_death_window_seconds`` the breaker trips, every unattended destructive
path stands down for ``mass_death_suspend_minutes``, and the operator is DM'd
once. Nothing is lost — autoclose and TTL timers stay armed, so once the
suspension lapses the sweeps re-evaluate against reality instead of against
the aftermath of an outage.

The suspension window is deliberately much longer than the detection window:
the damage in the incident happened seventeen minutes after the deaths, so a
breaker that reset in two minutes would have stood down and then destroyed the
topics anyway. Detection is fast, recovery is slow — that asymmetry is the
whole point.

Timekeeping is injected (``now``) so the behaviour is testable without sleeps.
"""

from __future__ import annotations

import time
from collections import deque

import structlog

from .config import config
from .i18n import t

logger = structlog.get_logger()

# Reason codes returned by `destruction_blocked`. Also metric label values.
BLOCKED_MASS_DEATH = "mass_death"

_deaths: deque[float] = deque()
_suspended_until: float = 0.0


def reset_for_testing() -> None:
    """Clear breaker state between tests."""
    global _suspended_until
    _deaths.clear()
    _suspended_until = 0.0


def _prune(now: float) -> None:
    """Drop deaths that have aged out of the detection window."""
    cutoff = now - config.mass_death_window_seconds
    while _deaths and _deaths[0] < cutoff:
        _deaths.popleft()


def destruction_blocked(*, now: float | None = None) -> str | None:
    """Reason automated destruction is currently suspended, else None.

    Only gates *unattended* actions — a user who taps "kill this session"
    during an outage still gets what they asked for.
    """
    if config.mass_death_threshold <= 0:
        return None
    moment = time.monotonic() if now is None else now
    return BLOCKED_MASS_DEATH if moment < _suspended_until else None


def note_window_death(*, now: float | None = None) -> bool:
    """Record one window death. Returns True when this death trips the breaker.

    Called from the single idempotent death path
    (``_handle_dead_window_notification``), so each window counts exactly once
    no matter whether the push stream or the poll loop noticed it first.
    """
    if config.mass_death_threshold <= 0:
        return False
    moment = time.monotonic() if now is None else now
    _prune(moment)
    _deaths.append(moment)
    if len(_deaths) < config.mass_death_threshold:
        return False

    global _suspended_until
    was_suspended = moment < _suspended_until
    _suspended_until = moment + config.mass_death_suspend_minutes * 60
    if was_suspended:
        # Still inside an episode — extend the suspension, stay quiet.
        return False
    logger.warning(
        "mass_death_breaker_tripped",
        deaths=len(_deaths),
        window_seconds=config.mass_death_window_seconds,
        suspend_minutes=config.mass_death_suspend_minutes,
    )
    return True


def format_breaker_alert(deaths: int) -> str:
    """Render the operator DM sent when the breaker trips."""
    return "\n".join(
        [
            t("🧯 *CCGram circuit breaker*"),
            "",
            t("{count} windows died within {seconds}s — this looks like an").format(
                count=deaths, seconds=config.mass_death_window_seconds
            ),
            t("infrastructure event, not something anyone asked for."),
            "",
            t(
                "Automatic topic/window cleanup is suspended for {minutes} minutes. "
                "Nothing is lost — the timers stay armed and re-evaluate afterwards."
            ).format(minutes=config.mass_death_suspend_minutes),
        ]
    )


async def note_window_death_and_alert(*, now: float | None = None) -> None:
    """Record a death and, if it trips the breaker, DM the operator once.

    Best-effort: never raises, so the death-notification path can't be broken
    by the breaker or its alerting.
    """
    if not note_window_death(now=now):
        return
    # Lazy: operator_alerts + destructive_audit pull telegram_client and i18n;
    # keep this module importable from the polling layer without them.
    from .destructive_audit import get_audit_client

    # Lazy: same cycle — operator_alerts is only needed on an actual trip.
    from .operator_alerts import SEVERITY_CRITICAL, notify_operator

    client = get_audit_client()
    if client is None:
        return
    await notify_operator(
        client, format_breaker_alert(len(_deaths)), severity=SEVERITY_CRITICAL
    )
