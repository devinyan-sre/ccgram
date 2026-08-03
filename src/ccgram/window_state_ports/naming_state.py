"""Naming-state feature port for managed topic/window display names."""

from __future__ import annotations

from dataclasses import dataclass

from ..window_state_store import window_store


@dataclass(frozen=True, slots=True)
class NamingProjection:
    """Read-only fields used by the provider-aware name allocator."""

    window_id: str
    cwd: str
    window_name: str
    provider_name: str
    auto_named: bool


def get_naming(window_id: str) -> NamingProjection | None:
    """Return naming state for one window, if tracked."""
    state = window_store.window_states.get(window_id)
    if state is None:
        return None
    return NamingProjection(
        window_id=window_id,
        cwd=state.cwd,
        window_name=state.window_name,
        provider_name=state.provider_name,
        auto_named=state.auto_named,
    )


def iter_naming() -> list[NamingProjection]:
    """Return stable naming snapshots for all tracked windows."""
    return [
        NamingProjection(
            window_id=window_id,
            cwd=state.cwd,
            window_name=state.window_name,
            provider_name=state.provider_name,
            auto_named=state.auto_named,
        )
        for window_id, state in window_store.window_states.items()
    ]


def set_auto_named(window_id: str, *, value: bool) -> None:
    """Mark whether ccgram owns this window's display name."""
    window_store.set_auto_named(window_id, value=value)


__all__ = ["NamingProjection", "get_naming", "iter_naming", "set_auto_named"]
