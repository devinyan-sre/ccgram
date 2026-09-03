"""Transactional provider handoff tests."""

from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ccgram.provider_handoff import _provider_ready, handoff_provider
from ccgram.provider_readiness import ProviderReadiness
from ccgram.topic_naming import ReservedTopicName


@asynccontextmanager
async def _reserved_name(*_args, **_kwargs):
    yield ReservedTopicName("project-2", automatic=True)


@pytest.fixture
def old_view(tmp_path: Path) -> MagicMock:
    return MagicMock(
        provider_name="claude",
        cwd=str(tmp_path),
        approval_mode="yolo",
    )


async def test_handoff_binds_only_after_replacement_is_ready(old_view) -> None:
    target = MagicMock()
    target.capabilities.name = "codex"
    target.capabilities.has_yolo_confirmation = False
    target.capabilities.chat_first_command_path = False
    with (
        patch(
            "ccgram.provider_handoff.window_query.view_window", return_value=old_view
        ),
        patch("ccgram.provider_handoff.get_provider_for_window", return_value=target),
        patch("ccgram.provider_handoff.reserve_topic_name", _reserved_name),
        patch(
            "ccgram.provider_handoff.resolve_launch_command",
            return_value="codex --yolo",
        ),
        patch("ccgram.provider_handoff.tmux_manager") as mux,
        patch("ccgram.provider_handoff.topic_orchestration") as orchestration,
        patch("ccgram.provider_handoff.session_manager") as sessions,
        patch("ccgram.provider_handoff.thread_router") as router,
        patch(
            "ccgram.provider_handoff._wait_until_ready",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "ccgram.provider_handoff.send_to_window",
            new=AsyncMock(return_value=(True, "sent")),
        ) as send,
    ):
        mux.create_window = AsyncMock(
            return_value=(True, "created", "project-2-2", "@new")
        )
        mux.stamp_pane_title = AsyncMock()
        mux.rename_window = AsyncMock(return_value=True)
        mux.find_window_by_id = AsyncMock(return_value=MagicMock())
        mux.kill_window = AsyncMock(return_value=True)

        result = await handoff_provider(
            user_id=7,
            thread_id=11,
            old_window_id="@old",
            target_provider="codex",
            context_prompt="continue here",
        )

    assert result.success is True
    assert result.new_window_id == "@new"
    assert result.context_sent is True
    orchestration.register_pending_creation.assert_called_once_with("@new")
    orchestration.clear_pending_creation.assert_called_once_with("@new")
    sessions.set_window_provider.assert_called_once_with("@new", "codex")
    sessions.set_window_auto_named.assert_called_once_with("@new", value=True)
    router.bind_thread.assert_called_once_with(7, 11, "@new", window_name="project-2")
    send.assert_awaited_once_with("@new", "continue here")
    mux.rename_window.assert_awaited_once_with("@new", "project-2")
    mux.kill_window.assert_awaited_once_with("@old")


async def test_handoff_uses_explicit_approval_mode_override(old_view) -> None:
    target = MagicMock()
    target.capabilities.name = "codex"
    target.capabilities.has_yolo_confirmation = False
    target.capabilities.chat_first_command_path = False
    with (
        patch(
            "ccgram.provider_handoff.window_query.view_window", return_value=old_view
        ),
        patch("ccgram.provider_handoff.get_provider_for_window", return_value=target),
        patch("ccgram.provider_handoff.reserve_topic_name", _reserved_name),
        patch(
            "ccgram.provider_handoff.resolve_launch_command",
            return_value="codex --yolo",
        ) as resolve_launch,
        patch("ccgram.provider_handoff.tmux_manager") as mux,
        patch("ccgram.provider_handoff.topic_orchestration"),
        patch("ccgram.provider_handoff.session_manager") as sessions,
        patch("ccgram.provider_handoff.thread_router"),
        patch(
            "ccgram.provider_handoff._wait_until_ready",
            new=AsyncMock(return_value=True),
        ),
    ):
        mux.create_window = AsyncMock(
            return_value=(True, "created", "project-2", "@new")
        )
        mux.stamp_pane_title = AsyncMock()
        mux.find_window_by_id = AsyncMock(return_value=MagicMock())
        mux.kill_window = AsyncMock(return_value=True)

        result = await handoff_provider(
            user_id=7,
            thread_id=11,
            old_window_id="@old",
            target_provider="codex",
            target_approval_mode="normal",
        )

    assert result.success is True
    resolve_launch.assert_called_once_with("codex", approval_mode="normal")
    sessions.set_window_approval_mode.assert_called_once_with("@new", "normal")


async def test_handoff_rejects_yolo_for_unsupported_provider(old_view) -> None:
    target = MagicMock()
    target.capabilities.name = "pi"
    with (
        patch(
            "ccgram.provider_handoff.window_query.view_window", return_value=old_view
        ),
        patch("ccgram.provider_handoff.get_provider_for_window", return_value=target),
        patch("ccgram.provider_handoff.tmux_manager") as mux,
    ):
        result = await handoff_provider(
            user_id=7,
            thread_id=11,
            old_window_id="@old",
            target_provider="pi",
            target_approval_mode="yolo",
        )

    assert result.success is False
    assert "does not support yolo" in result.message.lower()
    mux.create_window.assert_not_called()


async def test_codex_handoff_accepts_ready_tui_before_lazy_transcript() -> None:
    with (
        patch("ccgram.provider_handoff.tmux_manager") as mux,
        patch(
            "ccgram.provider_handoff.wait_for_provider_ready",
            new=AsyncMock(return_value=ProviderReadiness(True)),
        ),
        patch(
            "ccgram.provider_handoff.discover_and_register_transcript",
            new_callable=AsyncMock,
        ) as discover,
        patch(
            "ccgram.provider_handoff.read_session_map_raw",
            new_callable=AsyncMock,
        ) as read_map,
    ):
        mux.find_window_by_id = AsyncMock(return_value=MagicMock())

        ready = await _provider_ready("@new", "codex")

    assert ready is True
    discover.assert_not_awaited()
    read_map.assert_not_awaited()


async def test_handoff_startup_failure_rolls_back_and_keeps_binding(old_view) -> None:
    target = MagicMock()
    target.capabilities.name = "codex"
    target.capabilities.has_yolo_confirmation = False
    target.capabilities.chat_first_command_path = False
    with (
        patch(
            "ccgram.provider_handoff.window_query.view_window", return_value=old_view
        ),
        patch("ccgram.provider_handoff.get_provider_for_window", return_value=target),
        patch("ccgram.provider_handoff.reserve_topic_name", _reserved_name),
        patch("ccgram.provider_handoff.resolve_launch_command", return_value="codex"),
        patch("ccgram.provider_handoff.tmux_manager") as mux,
        patch("ccgram.provider_handoff.topic_orchestration") as orchestration,
        patch("ccgram.provider_handoff.session_manager"),
        patch("ccgram.provider_handoff.thread_router") as router,
        patch(
            "ccgram.provider_handoff._wait_until_ready",
            new=AsyncMock(return_value=False),
        ),
    ):
        mux.create_window = AsyncMock(
            return_value=(True, "created", "project-2", "@new")
        )
        mux.stamp_pane_title = AsyncMock()
        mux.kill_window = AsyncMock(return_value=True)

        result = await handoff_provider(
            user_id=7,
            thread_id=11,
            old_window_id="@old",
            target_provider="codex",
        )

    assert result.success is False
    assert "old session was kept" in result.message.lower()
    router.bind_thread.assert_not_called()
    mux.kill_window.assert_awaited_once_with("@new")
    orchestration.clear_pending_creation.assert_called_once_with("@new")


async def test_handoff_rejects_missing_project() -> None:
    view = MagicMock(provider_name="claude", cwd="/missing", approval_mode="normal")
    with patch("ccgram.provider_handoff.window_query.view_window", return_value=view):
        result = await handoff_provider(
            user_id=7,
            thread_id=11,
            old_window_id="@old",
            target_provider="codex",
        )
    assert result.success is False
    assert "directory" in result.message.lower()


async def test_handoff_context_failure_never_commits_binding(old_view) -> None:
    target = MagicMock()
    target.capabilities.name = "codex"
    target.capabilities.has_yolo_confirmation = False
    target.capabilities.chat_first_command_path = False
    with (
        patch(
            "ccgram.provider_handoff.window_query.view_window", return_value=old_view
        ),
        patch("ccgram.provider_handoff.get_provider_for_window", return_value=target),
        patch("ccgram.provider_handoff.reserve_topic_name", _reserved_name),
        patch("ccgram.provider_handoff.resolve_launch_command", return_value="codex"),
        patch("ccgram.provider_handoff.tmux_manager") as mux,
        patch("ccgram.provider_handoff.topic_orchestration"),
        patch("ccgram.provider_handoff.session_manager"),
        patch("ccgram.provider_handoff.thread_router") as router,
        patch(
            "ccgram.provider_handoff._wait_until_ready",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "ccgram.provider_handoff.send_to_window",
            new=AsyncMock(return_value=(False, "send failed")),
        ),
    ):
        mux.create_window = AsyncMock(
            return_value=(True, "created", "project-2", "@new")
        )
        mux.stamp_pane_title = AsyncMock()
        mux.kill_window = AsyncMock(return_value=True)

        result = await handoff_provider(
            user_id=7,
            thread_id=11,
            old_window_id="@old",
            target_provider="codex",
            context_prompt="continue here",
        )

    assert result.success is False
    assert "context could not be sent" in result.message.lower()
    router.bind_thread.assert_not_called()
    mux.kill_window.assert_awaited_once_with("@new")


async def test_handoff_old_cleanup_failure_restores_original_binding(old_view) -> None:
    target = MagicMock()
    target.capabilities.name = "codex"
    target.capabilities.has_yolo_confirmation = False
    target.capabilities.chat_first_command_path = False
    with (
        patch(
            "ccgram.provider_handoff.window_query.view_window", return_value=old_view
        ),
        patch("ccgram.provider_handoff.get_provider_for_window", return_value=target),
        patch("ccgram.provider_handoff.reserve_topic_name", _reserved_name),
        patch("ccgram.provider_handoff.resolve_launch_command", return_value="codex"),
        patch("ccgram.provider_handoff.tmux_manager") as mux,
        patch("ccgram.provider_handoff.topic_orchestration"),
        patch("ccgram.provider_handoff.session_manager"),
        patch("ccgram.provider_handoff.thread_router") as router,
        patch(
            "ccgram.provider_handoff._wait_until_ready",
            new=AsyncMock(return_value=True),
        ),
    ):
        mux.create_window = AsyncMock(
            return_value=(True, "created", "project-2", "@new")
        )
        mux.stamp_pane_title = AsyncMock()
        mux.find_window_by_id = AsyncMock(return_value=MagicMock())
        mux.kill_window = AsyncMock(side_effect=[False, True])

        result = await handoff_provider(
            user_id=7,
            thread_id=11,
            old_window_id="@old",
            target_provider="codex",
        )

    assert result.success is False
    assert "rolled back" in result.message.lower()
    assert router.bind_thread.call_args_list == [
        call(7, 11, "@new", window_name="project-2"),
        call(7, 11, "@old"),
    ]
    assert mux.kill_window.await_args_list == [call("@old"), call("@new")]
