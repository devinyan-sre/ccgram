"""Liveness progress tracking for the health gate.

The systemd watchdog gate originally asked only "are the background tasks
still objects that haven't finished?". That catches a crashed loop but not a
*wedged* one — a task blocked forever on a hung syscall, or spinning without
completing a cycle, stays ``done() == False`` and keeps the heartbeat flowing.

This module records a monotonic timestamp each time a loop finishes a cycle,
so the health gate can require actual forward progress. Loops call
:func:`record_progress`; the gate calls :func:`is_stalled`.

Bias: a false "unhealthy" costs a production restart, so the checks are
deliberately conservative — a component that has never reported progress is
treated as healthy (startup grace), and the default stall threshold is far
larger than any normal cycle time.
"""

from __future__ import annotations

import time

# component name → monotonic timestamp of its last completed cycle
_progress: dict[str, float] = {}

# Well-known component names, so the gate and the loops can't drift apart.
SESSION_MONITOR = "session_monitor"
STATUS_POLL = "status_poll"


def record_progress(component: str) -> None:
    """Stamp that ``component`` just completed a cycle."""
    _progress[component] = time.monotonic()


def seconds_since_progress(component: str) -> float | None:
    """Seconds since ``component`` last reported progress, or None if never."""
    last = _progress.get(component)
    if last is None:
        return None
    return time.monotonic() - last


def is_stalled(component: str, threshold_seconds: float) -> bool:
    """True when ``component`` has not progressed within the threshold.

    A component that has never reported progress is NOT stalled: during
    startup the loops have not completed their first cycle yet, and failing
    the gate there would restart the service in a boot loop.
    """
    elapsed = seconds_since_progress(component)
    if elapsed is None:
        return False
    return elapsed > threshold_seconds


def reset_for_testing() -> None:
    """Drop all recorded progress (test hook)."""
    _progress.clear()
