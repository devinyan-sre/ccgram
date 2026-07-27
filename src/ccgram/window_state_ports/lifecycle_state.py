"""Lifecycle-state feature port — window origin + user-detached flag.

Reads the project origin flag. Writes delegate to ``WindowStateStore``
setters which already validate input and schedule a single save per real
change.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..window_state_store import (
    DEFAULT_WINDOW_ORIGIN,
    WINDOW_ORIGINS,
    window_store,
)


@dataclass(frozen=True, slots=True)
class LifecycleProjection:
    """Read-only snapshot of lifecycle/origin flags for a window."""

    window_id: str
    origin: str
    user_detached: bool = False


def get_lifecycle(window_id: str) -> LifecycleProjection | None:
    """Lifecycle projection, or None if no state is tracked."""
    state = window_store.window_states.get(window_id)
    if state is None:
        return None
    origin = state.origin if state.origin in WINDOW_ORIGINS else DEFAULT_WINDOW_ORIGIN
    return LifecycleProjection(
        window_id=window_id, origin=origin, user_detached=state.user_detached
    )


def get_origin(window_id: str) -> str:
    """Origin string for a window. Defaults to manual_discovered."""
    state = window_store.window_states.get(window_id)
    if state is None:
        return DEFAULT_WINDOW_ORIGIN
    return state.origin if state.origin in WINDOW_ORIGINS else DEFAULT_WINDOW_ORIGIN


def set_window_origin(window_id: str, origin: str) -> None:
    """Set the lifecycle origin. Raises ValueError on unknown origin."""
    window_store.set_window_origin(window_id, origin)


def is_user_detached(window_id: str) -> bool:
    """True when the user detached this window with ``/unbind``.

    Such a window is exempt from the unbound-window TTL: ``/unbind`` promises
    the session keeps running, and the TTL exists to reap orphans of a failed
    bind flow, not sessions the user deliberately parked.
    """
    state = window_store.window_states.get(window_id)
    return bool(state and state.user_detached)


def set_user_detached(window_id: str, *, value: bool) -> None:
    """Mark or clear the user-detached (TTL-exempt) flag."""
    window_store.set_user_detached(window_id, value=value)


__all__ = [
    "LifecycleProjection",
    "get_lifecycle",
    "get_origin",
    "is_user_detached",
    "set_user_detached",
    "set_window_origin",
]
