"""Provider lifecycle Telegram command helpers."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.handlers.lifecycle_commands import (
    _diagnostic_problems,
    _park,
    approval_command,
    autoname_command,
    build_auth_failure_keyboard,
    ops_command,
)
from ccgram.i18n import _reset_language_for_testing
from ccgram.provider_handoff import HandoffResult
from ccgram.task_scheduler import TaskStats
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


def _approval_update(text: str) -> MagicMock:
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.chat.id = -100
    return update


async def test_approval_without_argument_reports_current_mode() -> None:
    update = _approval_update("/approval")
    view = MagicMock(provider_name="codex", approval_mode="normal")
    reply = AsyncMock()
    with (
        patch(
            "ccgram.handlers.lifecycle_commands._resolve_bound",
            return_value=(7, 11, "@3"),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.window_query.view_window",
            return_value=view,
        ),
        patch("ccgram.handlers.lifecycle_commands.safe_reply", reply),
    ):
        await approval_command(update, MagicMock())

    call_args = reply.await_args
    assert call_args is not None
    text = call_args.args[1]
    assert "codex" in text
    assert "/approval yolo" in text


async def test_approval_switches_transactionally_and_transfers_context() -> None:
    update = _approval_update("/approval yolo")
    context = MagicMock()
    view = MagicMock(provider_name="codex", approval_mode="normal")
    result = HandoffResult(
        True,
        "@3",
        new_window_id="@4",
        provider_name="codex",
        context_sent=True,
        window_name="project-codex-1",
    )
    reply = AsyncMock()
    with (
        patch(
            "ccgram.handlers.lifecycle_commands._resolve_bound",
            return_value=(7, 11, "@3"),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.window_query.view_window",
            return_value=view,
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.task_scheduler.views",
            return_value=[],
        ),
        patch("ccgram.handlers.lifecycle_commands.has_role", return_value=True),
        patch(
            "ccgram.handlers.lifecycle_commands.generate_handoff_context",
            new=AsyncMock(return_value="recent context"),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.handoff_provider",
            new=AsyncMock(return_value=result),
        ) as handoff,
        patch(
            "ccgram.handlers.lifecycle_commands._sync_topic_name",
            new=AsyncMock(),
        ) as sync_name,
        patch("ccgram.handlers.lifecycle_commands.safe_reply", reply),
    ):
        await approval_command(update, context)

    handoff.assert_awaited_once_with(
        user_id=7,
        thread_id=11,
        old_window_id="@3",
        target_provider="codex",
        context_prompt="recent context",
        target_approval_mode="yolo",
    )
    sync_name.assert_awaited_once()
    assert "YOLO" in reply.await_args_list[-1].args[1]


async def test_approval_refuses_to_interrupt_active_task() -> None:
    update = _approval_update("/approval 开启")
    view = MagicMock(provider_name="codex", approval_mode="normal")
    task = MagicMock(window_id="@3", task_id="T0042", state="active")
    reply = AsyncMock()
    with (
        patch(
            "ccgram.handlers.lifecycle_commands._resolve_bound",
            return_value=(7, 11, "@3"),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.window_query.view_window",
            return_value=view,
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.task_scheduler.views",
            return_value=[task],
        ),
        patch("ccgram.handlers.lifecycle_commands.has_role", return_value=True),
        patch(
            "ccgram.handlers.lifecycle_commands.handoff_provider",
            new_callable=AsyncMock,
        ) as handoff,
        patch("ccgram.handlers.lifecycle_commands.safe_reply", reply),
    ):
        await approval_command(update, MagicMock())

    handoff.assert_not_awaited()
    call_args = reply.await_args
    assert call_args is not None
    assert "T0042" in call_args.args[1]


async def test_approval_yolo_requires_admin_role() -> None:
    update = _approval_update("/approval yolo")
    view = MagicMock(provider_name="codex", approval_mode="normal")
    reply = AsyncMock()
    with (
        patch(
            "ccgram.handlers.lifecycle_commands._resolve_bound",
            return_value=(7, 11, "@3"),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.window_query.view_window",
            return_value=view,
        ),
        patch("ccgram.handlers.lifecycle_commands.has_role", return_value=False),
        patch(
            "ccgram.handlers.lifecycle_commands.handoff_provider",
            new_callable=AsyncMock,
        ) as handoff,
        patch("ccgram.handlers.lifecycle_commands.safe_reply", reply),
    ):
        await approval_command(update, MagicMock())

    handoff.assert_not_awaited()
    call_args = reply.await_args
    assert call_args is not None
    assert "YOLO" in call_args.args[1]


async def test_ops_reports_scheduler_timing_and_cancel_outcomes_in_chinese(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CCGRAM_LANG", "zh")
    _reset_language_for_testing()
    update = MagicMock()
    update.effective_user.id = 7
    update.message = MagicMock()
    reply = AsyncMock()
    with (
        patch(
            "ccgram.handlers.lifecycle_commands.config.is_user_allowed",
            return_value=True,
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.queue_snapshot",
            return_value=(1, 2, 3),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.delivery_outbox.snapshot",
            return_value=(0, 0),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.get_active_monitor",
            return_value=None,
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.task_scheduler.stats",
            return_value=TaskStats(1, 2, 1, 45, 12),
        ),
        patch(
            "ccgram.handlers.lifecycle_commands.cancellation_summary",
            return_value={
                "cancel_confirmed": 3,
                "cancel_timeout": 1,
                "force_cancelled": 2,
            },
        ),
        patch("ccgram.handlers.lifecycle_commands.safe_reply", reply),
    ):
        await ops_command(update, MagicMock())

    call = reply.await_args
    assert call is not None
    text = call.args[1]
    assert "ccgram 运维状态 · ⚠ 需要关注" in text
    assert "CLI 任务：运行 1 个 · 排队 2 个 · 取消确认中 1 个" in text
    assert "任务耗时：平均 45 秒 · 最久等待 12 秒" in text
    assert "取消统计（24 小时）：确认 3 个 · 超时 1 个 · 强制 2 个" in text
    _reset_language_for_testing()
