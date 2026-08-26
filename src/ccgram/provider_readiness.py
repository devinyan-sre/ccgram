"""Provider startup readiness and safe recovery for bound windows.

The multiplexer deliberately knows nothing about agent CLIs.  This module is
the provider-aware gate between a Telegram message and ``send_to_window``: it
waits until the expected CLI owns the pane, handles known startup prompts, and
can relaunch a tracked provider that has fallen back to a shell.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import Protocol

import structlog

from . import window_query
from .multiplexer import multiplexer as tmux_manager
from .providers import detect_provider_from_pane, resolve_launch_command

logger = structlog.get_logger()

_POLL_INTERVAL = 0.25
_CODEX_HEADER = "OpenAI Codex"
_CODEX_PROMPT = "›"
_CODEX_UPDATE_OPTION_RE = re.compile(
    r"^\s*(?:(?P<cursor>[›❯>])\s*)?(?P<number>\d+)\.\s+(?P<label>.+)$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    """Result of preparing a provider window for message delivery."""

    ready: bool
    reason: str = ""
    restarted: bool = False
    update_prompt_skipped: bool = False


@dataclass(frozen=True, slots=True)
class StrategyInspection:
    ready: bool
    reason: str = ""
    prompt_handled: bool = False


class ProviderReadinessStrategy(Protocol):
    """Extension point for provider-specific startup screens."""

    async def inspect(self, window_id: str, pane_text: str) -> StrategyInspection: ...


class DefaultReadinessStrategy:
    async def inspect(self, window_id: str, pane_text: str) -> StrategyInspection:
        del window_id, pane_text
        return StrategyInspection(True)


class CodexReadinessStrategy:
    async def inspect(self, window_id: str, pane_text: str) -> StrategyInspection:
        if _is_codex_update_prompt(pane_text):
            handled = await _skip_codex_update(window_id, pane_text)
            return StrategyInspection(
                False,
                "waiting for Codex after update prompt"
                if handled
                else "could not dismiss Codex update prompt",
                prompt_handled=handled,
            )
        if window_query.get_session_id_for_window(window_id) or _is_codex_tui_ready(
            pane_text
        ):
            return StrategyInspection(True)
        return StrategyInspection(False, "waiting for Codex prompt")


_DEFAULT_STRATEGY = DefaultReadinessStrategy()
_READINESS_STRATEGIES: dict[str, ProviderReadinessStrategy] = {
    "codex": CodexReadinessStrategy()
}


def register_readiness_strategy(
    provider_name: str, strategy: ProviderReadinessStrategy
) -> None:
    """Register or replace a provider startup readiness strategy."""
    _READINESS_STRATEGIES[provider_name] = strategy


def get_readiness_strategy(provider_name: str) -> ProviderReadinessStrategy:
    return _READINESS_STRATEGIES.get(provider_name, _DEFAULT_STRATEGY)


def _is_codex_update_prompt(pane_text: str) -> bool:
    """Return whether *pane_text* is Codex's blocking self-update chooser."""
    lowered = pane_text.lower()
    return (
        "update available" in lowered
        and "skip until next version" in lowered
        and "press enter to continue" in lowered
    )


def _is_codex_tui_ready(pane_text: str) -> bool:
    """Recognize the idle Codex TUI before its lazy transcript exists."""
    return _CODEX_HEADER in pane_text and _CODEX_PROMPT in pane_text


def _codex_update_navigation(pane_text: str) -> tuple[str, ...] | None:
    """Return keys that move the current update selection to durable skip."""
    options = list(_CODEX_UPDATE_OPTION_RE.finditer(pane_text))
    target_index = next(
        (
            index
            for index, option in enumerate(options)
            if "skip until next version" in option.group("label").lower()
        ),
        None,
    )
    if target_index is None or not options:
        return None
    current_index = next(
        (
            index
            for index, option in enumerate(options)
            if option.group("cursor") is not None
        ),
        0,
    )
    down_count = (target_index - current_index) % len(options)
    return (*("Down" for _ in range(down_count)), "Enter")


async def _skip_codex_update(window_id: str, pane_text: str) -> bool:
    """Select "Skip until next version" without allowing user text to do so."""
    navigation = _codex_update_navigation(pane_text)
    if navigation is None:
        logger.warning(
            "Could not parse Codex update options; refusing blind input",
            window_id=window_id,
        )
        return False
    for key in navigation:
        if not await tmux_manager.send_keys(window_id, key, enter=False, literal=False):
            return False
        await asyncio.sleep(0.1)
    logger.info("Skipped blocking Codex update prompt", window_id=window_id)
    return True


async def wait_for_provider_ready(  # noqa: C901 - readiness state machine
    window_id: str,
    provider_name: str,
    *,
    timeout: float = 15.0,
    restart_if_shell: bool = False,
) -> ProviderReadiness:
    """Wait until *provider_name* can safely receive a Telegram message.

    If a tracked non-shell provider has exited to a shell, ``restart_if_shell``
    relaunches it once using the persisted approval mode.  User text is never
    sent by this function, so a startup menu or shell cannot consume it.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout, 0.0)
    restarted = False
    update_prompt_skipped = False
    reason = "provider startup timed out"

    while True:
        window = await tmux_manager.find_window_by_id(window_id)
        if window is None:
            return ProviderReadiness(
                False,
                "window no longer exists",
                restarted=restarted,
                update_prompt_skipped=update_prompt_skipped,
            )

        detected = await detect_provider_from_pane(
            window.pane_current_command, window_id=window_id
        )

        if provider_name == "shell":
            if detected == "shell":
                return ProviderReadiness(True)
            reason = f"expected shell, found {detected or 'unknown process'}"
        elif detected == provider_name:
            pane_text = await tmux_manager.capture_pane(window_id) or ""
            inspection = await get_readiness_strategy(provider_name).inspect(
                window_id, pane_text
            )
            update_prompt_skipped |= inspection.prompt_handled
            if inspection.ready:
                return ProviderReadiness(
                    True,
                    restarted=restarted,
                    update_prompt_skipped=update_prompt_skipped,
                )
            reason = inspection.reason or f"waiting for {provider_name} prompt"
            if reason == "could not dismiss Codex update prompt":
                return ProviderReadiness(False, reason, restarted=restarted)
        elif detected == "shell" and restart_if_shell and not restarted:
            approval_mode = window_query.get_approval_mode(window_id)
            launch_command = resolve_launch_command(
                provider_name, approval_mode=approval_mode
            )
            if not launch_command:
                return ProviderReadiness(False, "provider has no launch command")
            logger.warning(
                "Provider exited to shell; relaunching before delivery",
                window_id=window_id,
                provider=provider_name,
            )
            if not await tmux_manager.send_keys(
                window_id, launch_command, enter=True, literal=True, raw=True
            ):
                return ProviderReadiness(False, "could not relaunch provider")
            restarted = True
            reason = f"waiting for relaunched {provider_name}"
        else:
            reason = f"expected {provider_name}, found {detected or 'unknown process'}"

        if loop.time() >= deadline:
            logger.warning(
                "Provider readiness timed out",
                window_id=window_id,
                provider=provider_name,
                reason=reason,
                restarted=restarted,
            )
            return ProviderReadiness(
                False,
                reason,
                restarted=restarted,
                update_prompt_skipped=update_prompt_skipped,
            )
        await asyncio.sleep(_POLL_INTERVAL)
