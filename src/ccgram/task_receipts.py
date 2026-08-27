"""Durable, provider-neutral Telegram acknowledgement lifecycle."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import structlog
from telegram import Message
from telegram.error import BadRequest, RetryAfter, TelegramError

from .i18n import t
from .inbound_store import inbound_store
from .telegram_client import TelegramClient
from .utils import task_done_callback
from .handlers.messaging_pipeline.message_sender import safe_edit, safe_reply

logger = structlog.get_logger()

_client: TelegramClient | None = None
_cleanup_tasks: set[asyncio.Task[None]] = set()
_DELETE_ATTEMPTS = 3


def _receipt_text(task_id: str) -> str:
    return t("✅ Received · task {task_id} · analyzing").format(task_id=task_id)


async def publish_task_receipt(
    message: Message,
    *,
    inbound_key: str,
    task_id: str,
    existing: Message | None = None,
) -> Message | None:
    """Send or promote a queue notice into the task's transient receipt."""
    receipt = existing
    if receipt is None:
        receipt = await safe_reply(message, _receipt_text(task_id))
    else:
        await safe_edit(receipt, _receipt_text(task_id))
    receipt_id = getattr(receipt, "message_id", None)
    if isinstance(receipt_id, int):
        inbound_store.set_receipt(inbound_key, receipt_id)
    return receipt


async def _cleanup_receipt(
    client: TelegramClient,
    *,
    key: str,
    chat_id: int,
    message_id: int,
) -> None:
    for attempt in range(_DELETE_ATTEMPTS):
        try:
            await client.delete_message(chat_id=chat_id, message_id=message_id)
            inbound_store.clear_receipt(key, message_id)
            return
        except BadRequest as exc:
            # Already deleted/expired is also a completed cleanup. Other bad
            # requests cannot be healed by retrying the same operation.
            inbound_store.clear_receipt(key, message_id)
            logger.info(
                "Task receipt no longer deletable",
                chat_id=chat_id,
                message_id=message_id,
                error=str(exc),
            )
            return
        except RetryAfter as exc:
            retry_after = exc.retry_after
            delay = (
                retry_after.total_seconds()
                if isinstance(retry_after, timedelta)
                else float(retry_after)
            )
            await asyncio.sleep(min(10.0, max(1.0, delay)))
        except TelegramError as exc:
            if attempt + 1 < _DELETE_ATTEMPTS:
                await asyncio.sleep(float(attempt + 1))
                continue
            # Deletion can be unavailable because of permissions. Leaving one
            # settled line is clearer than a permanent false "analyzing" state.
            try:
                await client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=t("✅ Task finished"),
                )
                inbound_store.clear_receipt(key, message_id)
            except TelegramError:
                logger.warning(
                    "Task receipt cleanup failed",
                    chat_id=chat_id,
                    message_id=message_id,
                    error=str(exc),
                )


async def finish_task_receipts(window_id: str) -> None:
    """Delete every transient receipt belonging to a completed window."""
    client = _client
    if client is None:
        return
    refs = inbound_store.receipt_refs_for_window(window_id)
    if not refs:
        return
    from .operations_dashboard import request_operations_dashboard_refresh

    for key, chat_id, thread_id, message_id in refs:
        await _cleanup_receipt(
            client,
            key=key,
            chat_id=chat_id,
            message_id=message_id,
        )
        request_operations_dashboard_refresh(chat_id, thread_id)


def _schedule_cleanup(window_id: str) -> None:
    try:
        task = asyncio.create_task(
            finish_task_receipts(window_id),
            name=f"task-receipt-cleanup:{window_id}",
        )
    except RuntimeError:
        logger.warning("No event loop available for task receipt cleanup")
        return
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)
    task.add_done_callback(task_done_callback)


def set_receipt_client(client: TelegramClient) -> None:
    """Arm cleanup and resume terminal receipts left across a restart."""
    global _client
    _client = client
    inbound_store.set_window_done_callback(_schedule_cleanup)
    for window_id in inbound_store.completed_receipt_windows():
        _schedule_cleanup(window_id)


def reset_for_testing() -> None:
    global _client
    _client = None
    inbound_store.set_window_done_callback(None)
    for task in tuple(_cleanup_tasks):
        task.cancel()
    _cleanup_tasks.clear()


__all__ = [
    "finish_task_receipts",
    "publish_task_receipt",
    "reset_for_testing",
    "set_receipt_client",
]
