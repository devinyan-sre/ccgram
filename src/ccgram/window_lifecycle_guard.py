"""Short-lived guards for transactional window lifecycle changes.

Window creation and replacement overlap with the session-map and topic polling
loops. These guards let observers distinguish an intentionally unbound or
recently retired window from a genuinely orphaned one.
"""

from __future__ import annotations

import time

_PENDING_CREATION_TTL_S = 30.0
_RETIRED_WINDOW_TTL_S = 120.0

_pending_window_creations: dict[str, float] = {}
_retired_windows: dict[str, float] = {}


def _register(registry: dict[str, float], window_id: str, ttl: float) -> None:
    if window_id:
        registry[window_id] = time.monotonic() + ttl


def _contains(registry: dict[str, float], window_id: str) -> bool:
    expires_at = registry.get(window_id)
    if expires_at is None:
        return False
    if time.monotonic() >= expires_at:
        registry.pop(window_id, None)
        return False
    return True


def register_pending_creation(window_id: str) -> None:
    """Protect a newly created window until its topic binding is committed."""
    _register(_pending_window_creations, window_id, _PENDING_CREATION_TTL_S)


def clear_pending_creation(window_id: str) -> None:
    """Release a pending-creation guard (idempotent)."""
    _pending_window_creations.pop(window_id, None)


def is_pending_creation(window_id: str) -> bool:
    """Return whether state/topic observers must preserve a new window."""
    return _contains(_pending_window_creations, window_id)


def register_retired_window(window_id: str) -> None:
    """Suppress stale discovery events after a transactional replacement."""
    _register(_retired_windows, window_id, _RETIRED_WINDOW_TTL_S)


def clear_retired_window(window_id: str) -> None:
    """Release a retirement guard after a handoff rollback."""
    _retired_windows.pop(window_id, None)


def is_auto_topic_suppressed(window_id: str) -> bool:
    """Return whether automatic topic creation must ignore this window."""
    return is_pending_creation(window_id) or _contains(_retired_windows, window_id)
