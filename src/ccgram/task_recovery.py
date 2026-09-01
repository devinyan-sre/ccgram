"""Restart recovery for durable inbound tasks and scheduler leases."""

from __future__ import annotations

import asyncio

import structlog

from .inbound_store import InboundItem, inbound_store
from .dispatch_confirmation import dispatch_confirmation
from . import window_query
from .multiplexer import multiplexer
from .multiplexer.window_ops import send_to_window
from .request_context import record_request
from .task_scheduler import task_scheduler
from .telegram_client import TelegramClient
from .thread_router import thread_router

logger = structlog.get_logger()
_recovery_tasks: set[asyncio.Task[None]] = set()


async def _recover_one(item: InboundItem) -> None:
    """Wait for scheduler capacity and replay one definitely queued item."""
    try:
        admission = await task_scheduler.acquire(
            chat_id=item.chat_id,
            thread_id=item.thread_id,
            user_id=item.user_id,
            window_id=item.window_id,
        )
        record_request(
            item.window_id,
            user_id=item.user_id,
            chat_id=item.chat_id,
            thread_id=item.thread_id,
            message_id=item.message_id,
            preserve_existing=admission.continuation,
        )
        dispatch_confirmation.begin(
            task_id=admission.task_id,
            chat_id=item.chat_id,
            thread_id=item.thread_id,
            user_id=item.user_id,
            window_id=item.window_id,
            provider=window_query.get_window_provider(item.window_id) or "unknown",
        )
        inbound_store.set_state(item.key, "dispatching")
        await dispatch_confirmation.mark_written(item.window_id)
        success, error = await send_to_window(item.window_id, item.text)
        if success:
            inbound_store.set_state(item.key, "forwarded")
            return
        dispatch_confirmation.complete(item.window_id)
        inbound_store.set_state(item.key, "failed")
        if not admission.continuation:
            await task_scheduler.release_window(
                item.window_id, outcome="recovery_failed"
            )
        logger.warning(
            "Could not recover queued inbound task",
            window_id=item.window_id,
            error=error,
        )
    except asyncio.CancelledError:
        raise
    except BaseException:
        # Leave a pre-dispatch item queued so the next clean restart can retry
        # it; an item already marked dispatching remains fail-closed/ambiguous.
        logger.exception("Durable task recovery failed", window_id=item.window_id)


def _track_recovery(task: asyncio.Task[None]) -> None:
    _recovery_tasks.add(task)
    task.add_done_callback(_recovery_tasks.discard)


async def recover_tasks(client: TelegramClient) -> tuple[int, int]:
    """Resume definitely queued messages and report ambiguous in-flight sends.

    A ``dispatching`` message may already have reached the CLI, so it is never
    replayed automatically. The operator receives an explicit resend notice.
    """
    task_scheduler.start_recovered_leases()
    interrupted = inbound_store.interrupt_ambiguous()
    for item in interrupted:
        await client.send_message(
            chat_id=item.chat_id,
            message_thread_id=item.thread_id,
            reply_to_message_id=item.message_id,
            allow_sending_without_reply=True,
            text=(
                "⚠️ ccgram restarted while this message was being dispatched. "
                "It was not replayed to avoid duplicate execution; resend it if "
                "the CLI did not receive it."
            ),
        )

    scheduled = 0
    for item in inbound_store.recoverable():
        if (
            thread_router.get_window_for_thread(item.user_id, item.thread_id)
            != item.window_id
            or await multiplexer.find_window_by_id(item.window_id) is None
        ):
            inbound_store.set_state(item.key, "failed")
            continue
        task = asyncio.create_task(
            _recover_one(item),
            name=f"task-recovery:{item.chat_id}:{item.thread_id}:{item.user_id}",
        )
        _track_recovery(task)
        scheduled += 1
    if scheduled or interrupted:
        logger.info(
            "Recovered durable task journal",
            queued_scheduled=scheduled,
            ambiguous_not_replayed=len(interrupted),
        )
    return scheduled, len(interrupted)


__all__ = ["recover_tasks"]
