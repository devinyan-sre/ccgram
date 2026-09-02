"""One-tap answer routing for interactive CLI prompts."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery

from ccgram.handlers.callback_data import CB_ASK_CHOICE
from ccgram.handlers.interactive.interactive_callbacks import (
    _handle_direct_choice,
    parse_direct_choice_callback,
)
from ccgram.handlers.interactive.interactive_ui import (
    _interactive_contexts,
    _interactive_mode,
    _interactive_msgs,
    _interactive_sequences,
)


@pytest.fixture(autouse=True)
def _clear_interactive_state():
    _interactive_contexts.clear()
    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_sequences.clear()
    yield
    _interactive_contexts.clear()
    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_sequences.clear()


def test_parse_choice_preserves_pane_target() -> None:
    assert parse_direct_choice_callback(f"{CB_ASK_CHOICE}2:7:@12|%5") == (
        "2",
        7,
        "@12",
        "%5",
    )


@pytest.mark.parametrize(
    "data",
    [
        f"{CB_ASK_CHOICE}9:1:@12",
        f"{CB_ASK_CHOICE}1:not-a-number:@12",
        f"{CB_ASK_CHOICE}1:1:",
    ],
)
def test_parse_choice_rejects_malformed_data(data: str) -> None:
    assert parse_direct_choice_callback(data) is None


async def test_current_numbered_choice_is_literal_without_enter() -> None:
    ikey = (10, 42)
    _interactive_mode[ikey] = "@12"
    _interactive_msgs[ikey] = 99
    _interactive_contexts[ikey] = (-100, 99)
    _interactive_sequences[ikey] = 7
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    window = MagicMock(window_id="@12")
    mux = MagicMock()
    mux.find_window_by_id = AsyncMock(return_value=window)
    mux.send_keys = AsyncMock(return_value=True)

    with patch("ccgram.handlers.interactive.interactive_callbacks.tmux_manager", mux):
        await _handle_direct_choice(
            query,
            10,
            42,
            -100,
            99,
            "@12",
            None,
            ("2", 7, "@12", None),
        )

    mux.send_keys.assert_awaited_once_with("@12", "2", enter=False, literal=True)
    query.answer.assert_awaited_once_with()
    assert _interactive_sequences[ikey] == 8


async def test_stale_or_double_tapped_choice_is_not_delivered() -> None:
    ikey = (10, 42)
    _interactive_mode[ikey] = "@12"
    _interactive_msgs[ikey] = 99
    _interactive_contexts[ikey] = (-100, 99)
    _interactive_sequences[ikey] = 8
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    mux = MagicMock()
    mux.send_keys = AsyncMock()

    with patch("ccgram.handlers.interactive.interactive_callbacks.tmux_manager", mux):
        await _handle_direct_choice(
            query,
            10,
            42,
            -100,
            99,
            "@12",
            None,
            ("2", 7, "@12", None),
        )

    mux.send_keys.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True
