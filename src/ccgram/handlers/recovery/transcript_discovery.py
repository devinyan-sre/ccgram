"""Transcript discovery for hookless providers.

Discovers and registers transcripts for providers without hook support
(Codex, Gemini). Also handles provider auto-detection from pane process
and shell ↔ agent transitions.

Key components:
  - discover_and_register_transcript: main discovery function called per topic
  - _detect_and_apply_provider: provider auto-detection from running process
  - _find_and_register_transcript: transcript search for hookless providers
"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ...metrics import BINDING_REPAIRS
from ...providers import (
    detect_provider_from_pane,
    detect_provider_from_runtime,
    detect_provider_from_transcript_path,
    get_cached_foreground_pgid,
    get_provider_for_window,
    should_probe_pane_title_for_provider_detection,
)
from ...session import session_manager
from ...session_map import session_map_prefix, session_map_sync
from ...telegram_client import TelegramClient
from ...multiplexer import multiplexer as tmux_manager
from ...window_state_ports import identity_state

if TYPE_CHECKING:
    from ...multiplexer.base import ForegroundInfo
    from ...providers.base import AgentProvider
    from ...multiplexer.base import WindowRef as TmuxWindow

logger = structlog.get_logger()

_CODEX_PROCESS_START_SKEW_SECS = 2.0
_CODEX_SESSION_START_WINDOW_SECS = 120.0


def _codex_process_time_bounds(
    provider_name: str, foreground: "ForegroundInfo | None"
) -> tuple[float | None, float | None]:
    """Return valid session start bounds for a fresh Codex process.

    Resume commands intentionally attach an older session, so they must not
    receive the fresh-process cutoff.
    """
    if (
        provider_name != "codex"
        or foreground is None
        or foreground.started_at <= 0
        or "resume" in foreground.argv
    ):
        return None, None
    return (
        max(0.0, foreground.started_at - _CODEX_PROCESS_START_SKEW_SECS),
        foreground.started_at + _CODEX_SESSION_START_WINDOW_SECS,
    )


def _binding_outside_process_bounds(
    identity: identity_state.IdentityProjection,
    not_before: float | None,
    not_after: float | None,
) -> bool:
    """Whether a Codex binding belongs to another process in the same cwd."""
    if (
        identity.provider_name != "codex"
        or (not_before is None and not_after is None)
        or not identity.transcript_path
    ):
        return False
    # Lazy: only the Codex recovery path needs to parse Codex session metadata.
    from ...providers.codex import codex_transcript_started_at

    started_at = codex_transcript_started_at(Path(identity.transcript_path))
    if started_at is None:
        return False
    return bool(
        (not_before is not None and started_at < not_before)
        or (not_after is not None and started_at > not_after)
    )


async def _codex_foreground(
    window_id: str,
    provider_name: str,
    window: "TmuxWindow | None",
) -> "ForegroundInfo | None":
    """Resolve foreground process details only for active Codex windows."""
    if window is None or provider_name != "codex":
        return None
    # Lazy: process detection pulls platform-specific process inspection; only
    # active Codex transcript disambiguation needs it.
    from ...providers.process_detection import foreground_cached

    return await foreground_cached(window_id)


def _session_id_already_bound(session_id: str, window_id: str) -> bool:
    """Return True if another currently bound window already uses ``session_id``."""
    # Lazy: thread_router may not be installed in some test paths; fail open
    # if it isn't available so discovery can still continue with this window.
    from ...thread_router import thread_router

    try:
        iterator = thread_router.iter_thread_bindings()
    except RuntimeError:
        return False

    for _user_id, _thread_id, bound_window_id in iterator:
        if bound_window_id == window_id:
            continue
        if identity_state.get_session_id(bound_window_id) == session_id:
            return True
    return False


def _is_agent_origin(
    window_id: str, identity: identity_state.IdentityProjection
) -> bool:
    """Return whether a shell transition means the original agent exited."""
    initial_provider = (
        identity_state.get_initial_provider_name(window_id) or identity.provider_name
    )
    return identity.provider_name not in ("", "shell") and initial_provider != "shell"


async def _detect_and_apply_provider(
    window_id: str,
    identity: identity_state.IdentityProjection,
    w: "TmuxWindow",
    *,
    client: TelegramClient | None = None,
    chat_id: int = 0,
    thread_id: int = 0,
) -> bool:
    """Apply provider transitions; report an agent-origin exit to shell."""
    if identity_state.is_provider_manually_overridden(window_id):
        return False
    detected = await detect_provider_from_pane(
        w.pane_current_command, window_id=window_id
    )
    if not detected and should_probe_pane_title_for_provider_detection(
        w.pane_current_command
    ):
        pane_title = await tmux_manager.get_pane_title(window_id)
        detected = detect_provider_from_runtime(
            w.pane_current_command,
            pane_title=pane_title,
        )

    if detected == "shell" and _is_agent_origin(window_id, identity):
        logger.info(
            "Agent exited to shell; keeping provider for safe recovery",
            window_id=window_id,
            provider=identity.provider_name,
        )
        return True

    if detected and detected != identity.provider_name:
        old_provider = identity.provider_name
        session_manager.set_window_provider(window_id, detected, cwd=w.cwd or None)
        # Lazy: providers/__init__.py reaches back into transcript code
        # via provider format modules.
        from ...providers import get_provider_for_window

        new_caps = get_provider_for_window(window_id, detected)
        old_caps = (
            get_provider_for_window(window_id, old_provider) if old_provider else None
        )
        if new_caps and new_caps.capabilities.chat_first_command_path:
            identity_state.clear_transcript_path(window_id)
            # Lazy: shell.shell_prompt_orchestrator hits the recovery
            # subpackage's discovery code via send-keys callbacks.
            from ..shell.shell_prompt_orchestrator import ensure_setup

            await ensure_setup(
                window_id,
                "provider_switch",
                client=client,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        elif old_caps and old_caps.capabilities.chat_first_command_path:
            # Lazy: same shell ↔ recovery cycle as above.
            from ..shell.shell_capture import clear_shell_monitor_state

            # Lazy: same shell ↔ recovery cycle as above.
            from ..shell.shell_prompt_orchestrator import (
                clear_state as clear_orchestrator,
            )

            clear_shell_monitor_state(window_id)
            clear_orchestrator(window_id)
    elif not detected and identity.transcript_path:
        inferred = detect_provider_from_transcript_path(str(identity.transcript_path))
        if inferred and inferred != identity.provider_name:
            session_manager.set_window_provider(window_id, inferred, cwd=w.cwd or None)
    return False


def _resolve_providers_to_try(
    window_id: str,
    identity: identity_state.IdentityProjection,
    w: "TmuxWindow | None",
) -> list[tuple[str, "AgentProvider"]] | None:
    """Determine which providers to probe for transcripts.

    Returns a list of (name, provider) pairs, or ``None`` to signal the
    caller should set up a shell provider.
    """
    # Lazy: hoisting forms polling/__init__ → window_tick →
    # recovery.transcript_discovery → polling_state partial-init
    # cycle (worker-order-dependent; verified during F6.2). polling_types
    # is leaf-level — Task 5 of Round 5 may hoist this once cycle test covers it.
    # Lazy: polling_types is leaf-pure; importing here at module load would touch the polling subpackage __init__
    from ..polling.polling_types import is_shell_prompt

    # Lazy: providers registry reaches back through transcripts
    from ...providers import registry

    if identity.provider_name:
        provider = get_provider_for_window(window_id, identity.provider_name)
        if provider.capabilities.chat_first_command_path:
            return []
        return [(provider.capabilities.name, provider)]

    if w and is_shell_prompt(w.pane_current_command):
        return None  # signals caller to set up shell

    return [
        (name, registry.get(name))
        for name in registry.provider_names()
        if not registry.get(name).capabilities.supports_hook and name != "shell"
    ]


async def _find_and_register_transcript(
    window_id: str,
    identity: identity_state.IdentityProjection,
    providers_to_try: list[tuple[str, "AgentProvider"]],
    pane_alive: bool,
    not_before: float | None = None,
    not_after: float | None = None,
    allow_bound_reassignment: bool = False,
) -> None:
    """Search for transcripts among candidate providers and register if found."""
    window_key = f"{session_map_prefix()}{window_id}"

    transcript_path_str = (
        str(identity.transcript_path) if identity.transcript_path else ""
    )

    for provider_name, provider in providers_to_try:
        max_age = 0 if pane_alive else None
        discover_kwargs: dict[str, float | None] = {"max_age": max_age}
        if provider_name == "codex" and not_before is not None:
            discover_kwargs["not_before"] = not_before
        if provider_name == "codex" and not_after is not None:
            discover_kwargs["not_after"] = not_after
        event = await asyncio.to_thread(
            provider.discover_transcript,
            identity.cwd,
            window_key,
            **discover_kwargs,
        )
        if not event:
            continue

        if not allow_bound_reassignment and _session_id_already_bound(
            event.session_id, window_id
        ):
            logger.debug(
                "Skipping discover result for window %s: session_id %s already bound",
                window_id,
                event.session_id,
            )
            continue

        if (
            identity.session_id == event.session_id
            and transcript_path_str == event.transcript_path
            and identity.provider_name == provider_name
        ):
            return

        session_map_sync.register_hookless_session(
            window_id=window_id,
            session_id=event.session_id,
            cwd=event.cwd,
            transcript_path=event.transcript_path,
            provider_name=provider_name,
        )
        if allow_bound_reassignment:
            BINDING_REPAIRS.inc(reason="process_time_window")
            logger.warning(
                "Repaired stale session binding for window %s: %s -> %s",
                window_id,
                identity.session_id,
                event.session_id,
            )
        await asyncio.to_thread(
            session_map_sync.write_hookless_session_map,
            window_id=window_id,
            session_id=event.session_id,
            cwd=event.cwd,
            transcript_path=event.transcript_path,
            provider_name=provider_name,
        )
        return


def _hook_already_resolved(
    window_id: str,
    identity: identity_state.IdentityProjection,
    *,
    not_before: float | None = None,
    not_after: float | None = None,
) -> bool:
    """True when a hookful provider has already populated transcript_path."""
    if not identity.provider_name:
        return False
    provider = get_provider_for_window(window_id, identity.provider_name)
    resolved = bool(provider.capabilities.supports_hook and identity.transcript_path)
    return resolved and not _binding_outside_process_bounds(
        identity, not_before, not_after
    )


def _foreground_process_restarted(
    *,
    before_pgid: int,
    after_pgid: int,
    old_identity: identity_state.IdentityProjection,
    new_identity: identity_state.IdentityProjection,
) -> bool:
    """True when the same provider is running in a new foreground process group."""
    return bool(
        before_pgid
        and after_pgid
        and before_pgid != after_pgid
        and old_identity.session_id
        and old_identity.provider_name
        and old_identity.provider_name == new_identity.provider_name
    )


async def _switch_to_shell(
    window_id: str,
    *,
    client: TelegramClient | None,
    chat_id: int,
    thread_id: int,
) -> None:
    """Provider-switch to shell and clear transcript bookkeeping."""
    session_manager.set_window_provider(window_id, "shell")
    identity_state.clear_transcript_path(window_id)
    # Lazy: same shell ↔ recovery cycle as _detect_and_apply_provider.
    from ..shell.shell_prompt_orchestrator import ensure_setup

    await ensure_setup(
        window_id,
        "provider_switch",
        client=client,
        chat_id=chat_id,
        thread_id=thread_id,
    )


async def discover_and_register_transcript(  # noqa: C901
    window_id: str,
    *,
    _window: "TmuxWindow | None" = None,
    client: TelegramClient | None = None,
    user_id: int = 0,
    thread_id: int = 0,
) -> None:
    """Discover and register transcript for hookless providers (Codex, Gemini).

    Also handles provider auto-detection from pane process name
    and shell ↔ agent transitions with prompt marker setup.
    """
    # Lazy: same polling/__init__ cycle as _resolve_providers_to_try.
    from ..polling.polling_types import is_shell_prompt

    # Lazy: thread_router proxy resolved when transcript discovery is invoked
    from ...thread_router import thread_router

    identity = identity_state.get_identity(window_id)
    if identity is None:
        return

    chat_id = thread_router.resolve_chat_id(user_id, thread_id) if user_id else 0

    w = _window or await tmux_manager.find_window_by_id(window_id)

    pgid_before = get_cached_foreground_pgid(window_id)
    original_identity = identity
    process_restarted = False
    if w and w.pane_current_command:
        agent_exited = await _detect_and_apply_provider(
            window_id, identity, w, client=client, chat_id=chat_id, thread_id=thread_id
        )
        if agent_exited:
            # Keep the provider identity intact. The text path's readiness
            # strategy can now relaunch the agent instead of treating input as
            # a shell command.
            return
        refreshed = identity_state.get_identity(window_id)
        if refreshed is None:
            return
        identity = refreshed
        pgid_after = get_cached_foreground_pgid(window_id)
        process_restarted = _foreground_process_restarted(
            before_pgid=pgid_before,
            after_pgid=pgid_after,
            old_identity=original_identity,
            new_identity=identity,
        )

    # Codex has no reliable SessionStart hook. Correlate its transcript
    # timestamp with the actual foreground process so another live window in
    # the same cwd cannot donate its session.
    foreground = await _codex_foreground(window_id, identity.provider_name, w)
    not_before, not_after = _codex_process_time_bounds(
        identity.provider_name, foreground
    )
    repairing_stale_binding = _binding_outside_process_bounds(
        identity, not_before, not_after
    )

    if (
        _hook_already_resolved(
            window_id,
            identity,
            not_before=not_before,
            not_after=not_after,
        )
        and not process_restarted
    ):
        return

    if not identity.cwd:
        if not w or not w.cwd:
            return
        session_manager.set_window_provider(
            window_id, identity.provider_name or "", cwd=w.cwd
        )
        refreshed = identity_state.get_identity(window_id)
        if refreshed is None:
            return
        identity = refreshed

    providers_to_try = _resolve_providers_to_try(window_id, identity, w)
    if providers_to_try is None:
        await _switch_to_shell(
            window_id, client=client, chat_id=chat_id, thread_id=thread_id
        )
        return
    if not providers_to_try:
        return

    pane_alive = w is not None and not is_shell_prompt(w.pane_current_command)
    await _find_and_register_transcript(
        window_id,
        identity,
        providers_to_try,
        pane_alive,
        not_before=not_before,
        not_after=not_after,
        allow_bound_reassignment=repairing_stale_binding,
    )
