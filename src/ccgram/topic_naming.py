"""Stable automatic names for provider-backed Telegram topics and windows."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import structlog

from .multiplexer import multiplexer as tmux_manager
from .thread_router import thread_router
from .window_state_ports.naming_state import (
    NamingProjection,
    get_naming,
    iter_naming,
)

logger = structlog.get_logger()

_MAX_PROJECT_CHARS = 72
_COMPONENT_RE = re.compile(r"[^\w.-]+", re.UNICODE)
_HERDR_TOPIC_SEPARATOR = " ▸ "
_reservations: set[str] = set()
_reservation_lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class ReservedTopicName:
    """A collision-safe display name and whether it is system-managed."""

    name: str
    automatic: bool


def _slug(value: str, fallback: str) -> str:
    cleaned = _COMPONENT_RE.sub("-", value.strip()).strip("-._")
    return cleaned or fallback


def project_slug(cwd: str) -> str:
    """Return a readable, bounded directory label for automatic names."""
    path = Path(cwd).expanduser()
    raw = path.name or "project"
    cleaned = _slug(raw, "project")
    if len(cleaned) <= _MAX_PROJECT_CHARS:
        return cleaned
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:8]
    return f"{cleaned[: _MAX_PROJECT_CHARS - 9]}-{digest}"


def automatic_name_prefix(cwd: str, provider_name: str) -> str:
    """Return ``<directory>-<provider>`` for a provider-backed topic."""
    provider = _slug(provider_name.lower(), "agent")
    return f"{project_slug(cwd)}-{provider}"


def _looks_system_managed(state: NamingProjection) -> bool:
    return state.auto_named


def _state_name(
    replacing_window_id: str,
) -> tuple[NamingProjection | None, str]:
    state = get_naming(replacing_window_id)
    if state is None:
        return None, ""
    name = state.window_name or thread_router.get_display_name(replacing_window_id)
    return state, _backend_label(name)


def _backend_label(name: str) -> str:
    """Strip herdr's adaptive workspace prefix from its underlying tab label."""
    return name.rsplit(_HERDR_TOPIC_SEPARATOR, 1)[-1].strip()


def _is_legacy_generated(state: NamingProjection) -> bool:
    """Recognize the old ``directory`` / ``directory-N`` backend names."""
    if state.auto_named or not state.cwd or not state.window_name:
        return False
    directory = project_slug(state.cwd)
    label = _backend_label(state.window_name)
    return bool(
        label == directory or re.fullmatch(rf"{re.escape(directory)}-\d+", label)
    )


def _window_order_key(state: NamingProjection) -> tuple[tuple[int, ...], str]:
    """Sort tmux and herdr identifiers by their numeric creation order."""
    return tuple(
        int(part) for part in re.findall(r"\d+", state.window_id)
    ), state.window_id


def _legacy_slot_names() -> dict[str, str]:
    """Assign deterministic provider-aware slots to unmigrated legacy names."""
    groups: dict[tuple[str, str], list[NamingProjection]] = {}
    for state in iter_naming():
        if not (state.auto_named or _is_legacy_generated(state)):
            continue
        key = (str(Path(state.cwd).expanduser()), state.provider_name.lower())
        groups.setdefault(key, []).append(state)

    slots: dict[str, str] = {}
    for (cwd, provider_name), states in groups.items():
        states.sort(key=_window_order_key)
        prefix = automatic_name_prefix(cwd, provider_name or "agent")
        for number, state in enumerate(states, start=1):
            if _is_legacy_generated(state):
                slots[state.window_id] = f"{prefix}-{number}"
    return slots


async def _used_names(exclude_window_id: str) -> set[str]:
    used = set(_reservations)
    legacy_slots = _legacy_slot_names()
    for state in iter_naming():
        if state.window_id != exclude_window_id and state.window_name:
            used.add(state.window_name)
            used.add(_backend_label(state.window_name))
        if state.window_id != exclude_window_id and state.window_id in legacy_slots:
            used.add(legacy_slots[state.window_id])
    for window_id, name in thread_router.window_display_names.items():
        if window_id != exclude_window_id and name:
            used.add(name)
            used.add(_backend_label(name))
    try:
        windows = await tmux_manager.list_windows()
    except Exception:  # noqa: BLE001 - persisted names remain a safe fallback
        logger.warning("Could not list live windows while allocating a topic name")
        windows = []
    for window in windows or []:
        if window.window_id != exclude_window_id and window.window_name:
            used.add(window.window_name)
            used.add(_backend_label(window.window_name))
    return used


def _next_numbered_name(prefix: str, used: set[str]) -> str:
    number = 1
    while f"{prefix}-{number}" in used:
        number += 1
    return f"{prefix}-{number}"


@asynccontextmanager
async def reserve_topic_name(
    cwd: str,
    provider_name: str,
    *,
    replacing_window_id: str = "",
    force_automatic: bool = False,
) -> AsyncIterator[ReservedTopicName]:
    """Reserve a name across concurrent create/recover/handoff operations.

    Manual and legacy names are preserved. ``force_automatic`` provides the
    explicit migration path for an existing topic.
    """
    async with _reservation_lock:
        state, current_name = _state_name(replacing_window_id)
        automatic = force_automatic or state is None or _looks_system_managed(state)
        used = await _used_names(replacing_window_id)
        prefix = automatic_name_prefix(cwd, provider_name)
        legacy_name = _legacy_slot_names().get(replacing_window_id, "")
        if force_automatic and legacy_name and legacy_name not in used:
            name = legacy_name
        elif (not automatic and current_name) or (
            current_name and re.fullmatch(rf"{re.escape(prefix)}-\d+", current_name)
        ):
            name = current_name
        else:
            name = _next_numbered_name(prefix, used)
        # A preserved manual name can still be reserved by another in-flight
        # replacement. Fall back to a readable numbered name in that rare case.
        if name in _reservations:
            name = _next_numbered_name(prefix, used)
            automatic = True
        _reservations.add(name)
    try:
        yield ReservedTopicName(name=name, automatic=automatic)
    finally:
        _reservations.discard(name)


__all__ = [
    "ReservedTopicName",
    "automatic_name_prefix",
    "project_slug",
    "reserve_topic_name",
]
