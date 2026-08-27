"""Operator alerts for stalled multi-operator task queues."""

from __future__ import annotations

import time

from .config import config
from .i18n import t
from .operator_alerts import notify_operator
from .task_scheduler import task_scheduler
from .telegram_client import TelegramClient

_last_alert: dict[tuple[int, int, int], float] = {}
_ALERT_COOLDOWN_SECONDS = 900


async def check_task_queue_alerts(client: TelegramClient) -> int:
    if config.task_queue_alert_seconds <= 0:
        return 0
    now = time.time()
    alerted = 0
    for view in task_scheduler.views():
        if view.state != "queued" or view.age_seconds < config.task_queue_alert_seconds:
            continue
        key = (view.chat_id, view.thread_id, view.user_id)
        if now - _last_alert.get(key, 0.0) < _ALERT_COOLDOWN_SECONDS:
            continue
        await notify_operator(
            client,
            t(
                "⚠️ *CCGram task queue stalled*\n\n"
                "topic `{thread_id}` · user `{user_id}` · "
                "waiting `{age}s` · position `{position}`"
            ).format(
                thread_id=view.thread_id,
                user_id=view.user_id,
                age=int(view.age_seconds),
                position=view.queue_position,
            ),
        )
        _last_alert[key] = now
        alerted += 1
    return alerted


def reset_for_testing() -> None:
    _last_alert.clear()


__all__ = ["check_task_queue_alerts", "reset_for_testing"]
