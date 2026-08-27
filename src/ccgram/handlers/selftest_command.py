"""Read-only end-to-end wiring checks that never invoke a CLI provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update

from ..config import config
from ..delivery_outbox import delivery_outbox
from ..i18n import t
from ..task_scheduler import task_scheduler
from .. import topic_routing
from .callback_helpers import get_thread_id
from .messaging_pipeline.message_queue import queue_snapshot
from .messaging_pipeline.message_sender import safe_reply

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


async def selftest_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/selftest`` — validate routing, scheduling and delivery state."""
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    thread_id = get_thread_id(update)
    chat_id = getattr(getattr(message, "chat", None), "id", 0)
    window_id = (
        topic_routing.resolve_window(user.id, thread_id)
        if thread_id is not None
        else None
    )
    pending, retrying = delivery_outbox.snapshot()
    topic_queues, queued, unfinished = queue_snapshot()
    active_tasks, waiting_tasks = task_scheduler.snapshot()
    dashboard_ready = config.dashboard_enabled and config.dashboard_scope in (
        "general",
        "topic",
        "both",
    )
    correlation_ready = bool(
        thread_id is not None
        and isinstance(chat_id, int)
        and chat_id < 0
        and window_id
    )
    health = not retrying
    lines = [
        t("🧪 CCGram self-test (no CLI request sent)"),
        t("{mark} Topic routing: {detail}").format(
            mark="✅" if window_id else "⚠️", detail=window_id or t("not bound")
        ),
        t("{mark} Request correlation: chat/member/topic/window").format(
            mark="✅" if correlation_ready else "⚠️"
        ),
        t("{mark} Scheduler: active {active} · queued {queued}").format(
            mark="✅", active=active_tasks, queued=waiting_tasks
        ),
        t("{mark} Delivery: pending {pending} · retrying {retrying}").format(
            mark="✅" if health else "⚠️", pending=pending, retrying=retrying
        ),
        t("{mark} Topic queues: {topics} · waiting {queued} · unfinished {unfinished}").format(
            mark="✅", topics=topic_queues, queued=queued, unfinished=unfinished
        ),
        t("{mark} Topic + General dashboard wiring").format(
            mark="✅" if dashboard_ready else "⚠️"
        ),
        t("Result: {result}").format(
            result=t("healthy") if health and correlation_ready else t("check warnings")
        ),
    ]
    await safe_reply(message, "\n".join(lines))


__all__ = ["selftest_command"]
