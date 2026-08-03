"""Provider lifecycle Telegram command helpers."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.handlers.lifecycle_commands import (
    _diagnostic_problems,
    _park,
    autoname_command,
    build_auth_failure_keyboard,
)
from ccgram.topic_naming import ReservedTopicName


@asynccontextmanager
async def _auto_name(*_args, **_kwargs):
    yield ReservedTopicName("ccgram-codex-1", automatic=True)


def test_diagnostic_problems_reports_cross_layer_mismatch() -> None:
    problems = _diagnostic_problems(
        alive=True,
        state_provider="claude",
        detected_provider="codex",
        transcript_provider="codex",
        session_id="",
    )
    assert problems == [
        "foreground provider differs from state",
        "transcript provider differs from state",
        "no session binding",
    ]


def test_auth_failure_keyboard_offers_codex_context_and_park() -> None:
    keyboard = build_auth_failure_keyboard("@7")
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Codex" in labels
    assert "Codex + context" in labels
    assert "Park topic" in labels


async def test_park_kills_window_but_preserves_topic_binding() -> None:
    with (
        patch("ccgram.handlers.lifecycle_commands.tmux_manager") as mux,
        patch("ccgram.handlers.lifecycle_commands.lifecycle_strategy") as lifecycle,
        patch("ccgram.handlers.lifecycle_commands.lifecycle_state") as state,
    ):
        mux.find_window_by_id = AsyncMock(return_value=object())
        mux.kill_window = AsyncMock(return_value=True)
        ok, message = await _park(7, 11, "@3")

    assert ok is True
    assert "preserved" in message
    mux.kill_window.assert_awaited_once_with("@3")
    state.set_parked.assert_called_once_with("@3", value=True)
    lifecycle.mark_dead_notified.assert_called_once_with(7, 11, "@3")


async def test_autoname_updates_parked_topic_without_live_window() -> None:
    update = MagicMock()
    update.message = MagicMock()
    context = MagicMock()
    view = MagicMock(cwd="/srv/ccgram", provider_name="codex")
    with (
        patch(
            "ccgram.handlers.lifecycle_commands._resolve_bound",
            return_value=(7, 11, "@3"),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.window_query.view_window",
            return_value=view,
        ),
        patch("ccgram.handlers.lifecycle_commands.reserve_topic_name", _auto_name),
        patch("ccgram.handlers.lifecycle_commands.tmux_manager") as mux,
        patch("ccgram.handlers.lifecycle_commands.session_manager") as sessions,
        patch(
            "ccgram.handlers.lifecycle_commands._sync_topic_name",
            new=AsyncMock(),
        ) as sync_name,
        patch("ccgram.handlers.lifecycle_commands.safe_reply", new=AsyncMock()),
    ):
        mux.find_window_by_id = AsyncMock(return_value=None)
        await autoname_command(update, context)

    mux.rename_window.assert_not_called()
    sessions.set_display_name.assert_called_once_with("@3", "ccgram-codex-1")
    sessions.set_window_auto_named.assert_called_once_with("@3", value=True)
    sync_name.assert_awaited_once()
