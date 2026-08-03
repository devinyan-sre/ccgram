"""Transactional provider replacement for an existing Telegram topic."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog

from .i18n import t
from .handlers.recovery.transcript_discovery import (
    discover_and_register_transcript,
)
from .handlers.topics import topic_orchestration
from .metrics import PROVIDER_HANDOFFS
from .multiplexer import multiplexer as tmux_manager
from .multiplexer.window_ops import send_to_window
from .providers import (
    detect_provider_from_pane,
    get_provider_for_window,
    has_yolo_mode,
    resolve_launch_command,
)
from .session import session_manager
from .session_map import read_session_map_raw, session_map_prefix
from .thread_router import thread_router
from .topic_naming import reserve_topic_name
from .window_state_store import CCGRAM_CREATED_WINDOW_ORIGIN
from . import window_query

logger = structlog.get_logger()

_START_TIMEOUT = 60.0
_START_POLL_INTERVAL = 0.5


def _record_handoff(source: str, target: str, outcome: str) -> None:
    PROVIDER_HANDOFFS.inc(source=source, target=target, outcome=outcome)


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """Outcome of a provider handoff attempt."""

    success: bool
    old_window_id: str
    new_window_id: str = ""
    provider_name: str = ""
    message: str = ""
    context_sent: bool = False
    window_name: str = ""


async def _provider_ready(window_id: str, provider_name: str) -> bool:
    window = await tmux_manager.find_window_by_id(window_id)
    if window is None:
        return False

    detected = await detect_provider_from_pane(
        window.pane_current_command, window_id=window_id
    )
    if detected and detected != provider_name:
        return False
    if provider_name == "shell":
        return True

    await discover_and_register_transcript(window_id, _window=window)
    raw = await read_session_map_raw()
    if not raw:
        return False
    info = raw.get(f"{session_map_prefix()}{window_id}")
    return bool(isinstance(info, dict) and info.get("session_id"))


async def _wait_until_ready(window_id: str, provider_name: str) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_TIMEOUT
    while loop.time() < deadline:
        if await _provider_ready(window_id, provider_name):
            return True
        await asyncio.sleep(_START_POLL_INTERVAL)
    return False


async def handoff_provider(  # noqa: C901,PLR0911,PLR0912,PLR0915 - transaction
    *,
    user_id: int,
    thread_id: int,
    old_window_id: str,
    target_provider: str,
    context_prompt: str = "",
) -> HandoffResult:
    """Replace a topic's provider, retaining the old binding until ready.

    The replacement window is hidden from automatic topic creation while it
    starts. Only after its process and transcript are ready is the topic rebound
    and the old window terminated. Any failure removes the replacement and
    leaves the original topic/window untouched.
    """
    old_view = window_query.view_window(old_window_id)
    source_provider = old_view.provider_name if old_view else "unknown"
    if old_view is None or not old_view.cwd or not Path(old_view.cwd).is_dir():
        _record_handoff(source_provider, target_provider, "invalid_source")
        return HandoffResult(
            False,
            old_window_id,
            provider_name=target_provider,
            message=t("The original project directory is unavailable."),
        )

    # Forces provider registration and validates the name.
    target = get_provider_for_window("", provider_name=target_provider)
    if target.capabilities.name != target_provider:
        _record_handoff(source_provider, target_provider, "invalid_target")
        return HandoffResult(
            False,
            old_window_id,
            provider_name=target_provider,
            message=t("Unknown provider: {provider}").format(provider=target_provider),
        )

    approval_mode = (
        old_view.approval_mode if has_yolo_mode(target_provider) else "normal"
    )
    launch_command = resolve_launch_command(
        target_provider, approval_mode=approval_mode
    )
    async with reserve_topic_name(
        old_view.cwd,
        target_provider,
        replacing_window_id=old_window_id,
    ) as reserved_name:
        success, message, window_name, new_window_id = await tmux_manager.create_window(
            old_view.cwd,
            window_name=reserved_name.name,
            launch_command=launch_command,
        )
        if success:
            topic_orchestration.register_pending_creation(new_window_id)
            session_manager.set_window_origin(
                new_window_id, CCGRAM_CREATED_WINDOW_ORIGIN
            )
            session_manager.set_window_cwd(new_window_id, old_view.cwd)
            session_manager.set_window_provider(new_window_id, target_provider)
            session_manager.set_window_approval_mode(new_window_id, approval_mode)
            session_manager.set_window_auto_named(
                new_window_id, value=reserved_name.automatic
            )
            session_manager.set_display_name(new_window_id, window_name)
    if not success:
        _record_handoff(source_provider, target_provider, "create_failed")
        return HandoffResult(
            False,
            old_window_id,
            provider_name=target_provider,
            message=message,
        )

    binding_committed = False
    try:
        await tmux_manager.stamp_pane_title(new_window_id, target_provider)

        if approval_mode == "yolo" and target.capabilities.has_yolo_confirmation:
            # Lazy: this prompt only exists on providers that declare it.
            from .handlers.topics.window_launch_service import (
                _accept_yolo_confirmation,
            )

            await _accept_yolo_confirmation(new_window_id)

        if not await _wait_until_ready(new_window_id, target_provider):
            await tmux_manager.kill_window(new_window_id)
            _record_handoff(source_provider, target_provider, "startup_failed")
            return HandoffResult(
                False,
                old_window_id,
                provider_name=target_provider,
                message=t(
                    "{provider} did not become ready within {seconds} seconds. "
                    "The old session was kept."
                ).format(provider=target_provider, seconds=int(_START_TIMEOUT)),
            )

        # Backends may suffix the requested name while the old window still
        # exists. Both tmux and herdr support duplicate labels during this
        # short transactional overlap, so finalize before the commit point.
        if window_name != reserved_name.name:
            if not await tmux_manager.rename_window(new_window_id, reserved_name.name):
                await tmux_manager.kill_window(new_window_id)
                _record_handoff(source_provider, target_provider, "rename_failed")
                return HandoffResult(
                    False,
                    old_window_id,
                    provider_name=target_provider,
                    message=t(
                        "The replacement started, but its topic name could not "
                        "be finalized. The old session was kept."
                    ),
                )
            window_name = reserved_name.name
            session_manager.set_display_name(new_window_id, window_name)

        if target.capabilities.chat_first_command_path:
            # Lazy: only shell-like providers need prompt markers.
            from .handlers.shell.shell_prompt_orchestrator import ensure_setup

            await ensure_setup(new_window_id, "auto")
        context_sent = False
        if context_prompt:
            context_sent, _ = await send_to_window(new_window_id, context_prompt)
            if not context_sent:
                await tmux_manager.kill_window(new_window_id)
                _record_handoff(source_provider, target_provider, "context_failed")
                return HandoffResult(
                    False,
                    old_window_id,
                    provider_name=target_provider,
                    message=t(
                        "The replacement started, but context could not be sent. "
                        "The old session was kept."
                    ),
                )

        # Commit only after all replacement preparation has succeeded.
        thread_router.bind_thread(
            user_id, thread_id, new_window_id, window_name=window_name
        )
        binding_committed = True

        old_exists = await tmux_manager.find_window_by_id(old_window_id) is not None
        if old_exists and not await tmux_manager.kill_window(old_window_id):
            thread_router.bind_thread(user_id, thread_id, old_window_id)
            binding_committed = False
            await tmux_manager.kill_window(new_window_id)
            _record_handoff(source_provider, target_provider, "old_cleanup_failed")
            return HandoffResult(
                False,
                old_window_id,
                provider_name=target_provider,
                message=t(
                    "The old session could not be stopped, so the switch was "
                    "rolled back."
                ),
            )

        _record_handoff(source_provider, target_provider, "ok")
        logger.info(
            "Provider handoff complete: thread=%s %s(%s) -> %s(%s)",
            thread_id,
            source_provider,
            old_window_id,
            target_provider,
            new_window_id,
        )
        return HandoffResult(
            True,
            old_window_id,
            new_window_id=new_window_id,
            provider_name=target_provider,
            message=t("Switched to {provider}.").format(provider=target_provider),
            context_sent=context_sent,
            window_name=window_name,
        )
    except asyncio.CancelledError:
        if binding_committed:
            thread_router.bind_thread(user_id, thread_id, old_window_id)
        await tmux_manager.kill_window(new_window_id)
        _record_handoff(source_provider, target_provider, "cancelled")
        raise
    except Exception:
        logger.exception(
            "Provider handoff failed; rolling back replacement window %s",
            new_window_id,
        )
        if binding_committed:
            thread_router.bind_thread(user_id, thread_id, old_window_id)
        await tmux_manager.kill_window(new_window_id)
        _record_handoff(source_provider, target_provider, "error")
        return HandoffResult(
            False,
            old_window_id,
            provider_name=target_provider,
            message=t("Provider handoff failed. The old session was kept."),
        )
    finally:
        topic_orchestration.clear_pending_creation(new_window_id)
