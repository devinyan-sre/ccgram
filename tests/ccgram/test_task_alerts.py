from unittest.mock import AsyncMock, patch

from ccgram.task_alerts import check_task_queue_alerts, reset_for_testing
from ccgram.task_scheduler import TaskView
from ccgram.telegram_client import FakeTelegramClient


async def test_stalled_queue_alerts_operator_once_per_cooldown() -> None:
    reset_for_testing()
    queued = TaskView(
        chat_id=-100,
        thread_id=7,
        user_id=10,
        window_id="@1",
        state="queued",
        age_seconds=600,
        supplements=0,
        queue_position=2,
    )
    with (
        patch("ccgram.task_alerts.config.task_queue_alert_seconds", 300),
        patch("ccgram.task_alerts.task_scheduler.views", return_value=[queued]),
        patch("ccgram.task_alerts.notify_operator", new_callable=AsyncMock) as notify,
    ):
        assert await check_task_queue_alerts(FakeTelegramClient()) == 1
        assert await check_task_queue_alerts(FakeTelegramClient()) == 0

    notify.assert_awaited_once()
