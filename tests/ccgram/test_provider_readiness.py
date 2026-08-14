"""Regression tests for provider startup gating and safe relaunch."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ccgram.provider_readiness import (
    _codex_update_navigation,
    wait_for_provider_ready,
)

_MOD = "ccgram.provider_readiness"


def _window(command: str) -> MagicMock:
    return MagicMock(pane_current_command=command)


def test_update_navigation_uses_current_cursor_and_target_label() -> None:
    pane = (
        "1. Update now\n› 2. Skip\n3. Skip until next version\nPress enter to continue"
    )

    assert _codex_update_navigation(pane) == ("Down", "Enter")


def test_update_navigation_fails_closed_when_menu_shape_is_unknown() -> None:
    assert _codex_update_navigation("Update available! Press enter") is None


@pytest.mark.asyncio
async def test_codex_tui_is_ready_before_lazy_transcript_exists() -> None:
    with (
        patch(f"{_MOD}.tmux_manager") as mux,
        patch(
            f"{_MOD}.detect_provider_from_pane",
            new_callable=AsyncMock,
            return_value="codex",
        ),
        patch(f"{_MOD}.window_query") as state,
    ):
        mux.find_window_by_id = AsyncMock(return_value=_window("codex"))
        mux.capture_pane = AsyncMock(
            return_value="OpenAI Codex (v0.147.0)\n\n› Improve documentation"
        )
        state.get_session_id_for_window.return_value = None

        result = await wait_for_provider_ready("@1", "codex", timeout=0)

    assert result.ready is True


@pytest.mark.asyncio
async def test_codex_existing_session_is_ready_without_visible_header() -> None:
    with (
        patch(f"{_MOD}.tmux_manager") as mux,
        patch(
            f"{_MOD}.detect_provider_from_pane",
            new_callable=AsyncMock,
            return_value="codex",
        ),
        patch(f"{_MOD}.window_query") as state,
    ):
        mux.find_window_by_id = AsyncMock(return_value=_window("codex"))
        mux.capture_pane = AsyncMock(return_value="old scrollback")
        state.get_session_id_for_window.return_value = "session-1"

        result = await wait_for_provider_ready("@1", "codex", timeout=0)

    assert result.ready is True


@pytest.mark.asyncio
async def test_codex_update_prompt_is_skipped_before_ready() -> None:
    update_prompt = (
        "Update available! 0.147.0\n"
        "1. Update now\n2. Remind me later\n3. Skip until next version\n"
        "Press enter to continue"
    )
    with (
        patch(f"{_MOD}.tmux_manager") as mux,
        patch(
            f"{_MOD}.detect_provider_from_pane",
            new_callable=AsyncMock,
            return_value="codex",
        ),
        patch(f"{_MOD}.window_query") as state,
    ):
        mux.find_window_by_id = AsyncMock(return_value=_window("codex"))
        mux.capture_pane = AsyncMock(
            side_effect=[
                update_prompt,
                "OpenAI Codex (v0.147.0)\n› Start a task",
            ]
        )
        mux.send_keys = AsyncMock(return_value=True)
        state.get_session_id_for_window.return_value = None

        result = await wait_for_provider_ready("@1", "codex", timeout=2)

    assert result.ready is True
    assert result.update_prompt_skipped is True
    assert mux.send_keys.await_args_list == [
        call("@1", "Down", enter=False, literal=False),
        call("@1", "Down", enter=False, literal=False),
        call("@1", "Enter", enter=False, literal=False),
    ]


@pytest.mark.asyncio
async def test_bound_provider_at_shell_is_relaunched_before_ready() -> None:
    with (
        patch(f"{_MOD}.tmux_manager") as mux,
        patch(
            f"{_MOD}.detect_provider_from_pane",
            new_callable=AsyncMock,
            side_effect=["shell", "codex"],
        ),
        patch(f"{_MOD}.window_query") as state,
        patch(
            f"{_MOD}.resolve_launch_command",
            return_value="codex --dangerously-bypass-approvals-and-sandbox",
        ),
    ):
        mux.find_window_by_id = AsyncMock(
            side_effect=[_window("bash"), _window("codex")]
        )
        mux.capture_pane = AsyncMock(return_value="OpenAI Codex\n› Start a task")
        mux.send_keys = AsyncMock(return_value=True)
        state.get_approval_mode.return_value = "yolo"
        state.get_session_id_for_window.return_value = None

        result = await wait_for_provider_ready(
            "@1", "codex", timeout=2, restart_if_shell=True
        )

    assert result.ready is True
    assert result.restarted is True
    mux.send_keys.assert_awaited_once_with(
        "@1",
        "codex --dangerously-bypass-approvals-and-sandbox",
        enter=True,
        literal=True,
        raw=True,
    )


@pytest.mark.asyncio
async def test_provider_mismatch_fails_without_typing_anything() -> None:
    with (
        patch(f"{_MOD}.tmux_manager") as mux,
        patch(
            f"{_MOD}.detect_provider_from_pane",
            new_callable=AsyncMock,
            return_value="shell",
        ),
    ):
        mux.find_window_by_id = AsyncMock(return_value=_window("bash"))
        mux.send_keys = AsyncMock(return_value=True)

        result = await wait_for_provider_ready(
            "@1", "codex", timeout=0, restart_if_shell=False
        )

    assert result.ready is False
    assert "expected codex" in result.reason
    mux.send_keys.assert_not_awaited()
