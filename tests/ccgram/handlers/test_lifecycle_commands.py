"""Provider lifecycle Telegram command helpers."""

from unittest.mock import AsyncMock, patch

from ccgram.handlers.lifecycle_commands import (
    _diagnostic_problems,
    _park,
    build_auth_failure_keyboard,
)


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
