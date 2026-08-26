from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.task_cancellation import force_cancel, graceful_cancel
from ccgram.task_scheduler import CancelRequest, TaskView


def _view() -> TaskView:
    return TaskView(
        chat_id=-100,
        thread_id=7,
        user_id=10,
        window_id="@1",
        state="cancelling",
        age_seconds=2,
        supplements=0,
        task_id="T0001",
    )


async def test_graceful_cancel_releases_only_after_idle_confirmation() -> None:
    scheduler = MagicMock()
    scheduler.request_cancel = AsyncMock(
        return_value=CancelRequest("requested", "T0001", "@1", 10)
    )
    scheduler.views.return_value = [_view()]
    scheduler.confirm_cancel = AsyncMock(return_value=True)
    with (
        patch("ccgram.task_cancellation.task_scheduler", scheduler),
        patch("ccgram.task_cancellation.multiplexer") as mux,
        patch(
            "ccgram.task_cancellation._cli_is_idle", new=AsyncMock(return_value=True)
        ),
        patch("ccgram.task_cancellation.inbound_store"),
        patch("ccgram.task_cancellation.clear_window"),
        patch("ccgram.task_cancellation.record_task_audit"),
    ):
        mux.send_keys = AsyncMock(return_value=True)
        result = await graceful_cancel(
            chat_id=-100, thread_id=7, requester_user_id=10, task_id="T0001"
        )

    assert result.status == "cancel_confirmed"
    scheduler.confirm_cancel.assert_awaited_once_with("T0001")


async def test_graceful_cancel_timeout_keeps_cancelling_task_active() -> None:
    scheduler = MagicMock()
    scheduler.request_cancel = AsyncMock(
        return_value=CancelRequest("requested", "T0001", "@1", 10)
    )
    scheduler.views.return_value = [_view()]
    scheduler.confirm_cancel = AsyncMock(return_value=True)
    with (
        patch("ccgram.task_cancellation.task_scheduler", scheduler),
        patch("ccgram.task_cancellation.multiplexer") as mux,
        patch("ccgram.task_cancellation.config.task_cancel_confirm_seconds", 0),
        patch("ccgram.task_cancellation.record_task_audit"),
    ):
        mux.send_keys = AsyncMock(return_value=True)
        result = await graceful_cancel(
            chat_id=-100, thread_id=7, requester_user_id=10, task_id="T0001"
        )

    assert result.status == "cancel_timeout"
    scheduler.confirm_cancel.assert_not_awaited()


async def test_force_cancel_kills_window_then_confirms_scheduler() -> None:
    scheduler = MagicMock()
    scheduler.request_cancel = AsyncMock(
        return_value=CancelRequest("already_cancelling", "T0001", "@1", 20)
    )
    scheduler.confirm_cancel = AsyncMock(return_value=True)
    with (
        patch("ccgram.task_cancellation.task_scheduler", scheduler),
        patch("ccgram.task_cancellation.multiplexer") as mux,
        patch("ccgram.task_cancellation.inbound_store"),
        patch("ccgram.task_cancellation.clear_window"),
        patch("ccgram.task_cancellation.record_task_audit"),
    ):
        mux.find_window_by_id = AsyncMock(return_value=object())
        mux.kill_window = AsyncMock(return_value=True)
        result = await force_cancel(
            chat_id=-100,
            thread_id=7,
            requester_user_id=99,
            task_id="T0001",
        )

    assert result.status == "force_cancelled"
    mux.kill_window.assert_awaited_once_with("@1")
    scheduler.confirm_cancel.assert_awaited_once_with("T0001", forced=True)
