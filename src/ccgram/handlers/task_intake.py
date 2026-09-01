"""Provider-neutral admission and Telegram request correlation."""

from __future__ import annotations

import asyncio

import structlog
from telegram import Message

from ..dispatch_confirmation import dispatch_confirmation
from ..i18n import t
from ..inbound_store import inbound_store
from ..operations_dashboard import request_operations_dashboard_refresh
from ..request_context import record_request
from ..task_scheduler import (
    TaskAdmission,
    TaskCancellingError,
    TaskQueueCancelledError,
    TaskStuckError,
    TaskSupplementLimitError,
    task_scheduler,
)
from ..task_receipts import publish_task_receipt
from .messaging_pipeline.message_sender import safe_edit, safe_reply

logger = structlog.get_logger()


def message_ids(message: Message) -> tuple[int, int]:
    """Return concrete IDs while tolerating lightweight unit-test doubles."""
    nested_chat_id = getattr(getattr(message, "chat", None), "id", None)
    direct_chat_id = getattr(message, "chat_id", None)
    chat_id = (
        nested_chat_id
        if isinstance(nested_chat_id, int)
        else (direct_chat_id if isinstance(direct_chat_id, int) else 0)
    )
    raw_message_id = getattr(message, "message_id", None)
    message_id = raw_message_id if isinstance(raw_message_id, int) else id(message)
    return chat_id, message_id


async def admit_request(
    *,
    window_id: str,
    user_id: int,
    thread_id: int,
    message: Message,
    dispatch_text: str,
) -> TaskAdmission | None:
    """Admit one task through the shared scheduler before provider dispatch."""
    chat_id, message_id = message_ids(message)
    unresolved = dispatch_confirmation.unresolved_for_operator(
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        window_id=window_id,
    )
    if unresolved is not None:
        await safe_reply(
            message,
            f"❌ 上一任务 `{unresolved.task_id}` 尚未被 CLI 确认。为避免两次问题串联，"
            f"本条未发送。请使用 `/task_retry {unresolved.task_id}` 或取消任务。",
        )
        return None
    if not inbound_store.stage(
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        message_id=message_id,
        window_id=window_id,
        text=dispatch_text,
    ):
        logger.info(
            "Dropped duplicate inbound message",
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            message_id=message_id,
        )
        return None

    admission_task = asyncio.create_task(
        task_scheduler.acquire(
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            window_id=window_id,
        )
    )
    inbound_key = inbound_store.make_key(chat_id, thread_id, message_id)
    queue_message: Message | None = None
    await asyncio.sleep(0)
    if not admission_task.done():
        position = await task_scheduler.queue_position(
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
        )
        queued_view = next(
            (
                view
                for view in task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
                if view.user_id == user_id and view.state == "queued"
            ),
            None,
        )
        queue_message = await safe_reply(
            message,
            t(
                "⏳ Task {task_id} queued (position {position}, estimated ≤{eta}s)."
            ).format(
                task_id=queued_view.task_id if queued_view else "-",
                position=max(1, position),
                eta=queued_view.estimated_wait_seconds if queued_view else 0,
            ),
        )
        request_operations_dashboard_refresh(chat_id, thread_id)
    try:
        admission = await admission_task
    except TaskSupplementLimitError:
        inbound_store.set_state(inbound_key, "failed")
        await safe_reply(
            message,
            t(
                "❌ This task has too many supplements. Use /task_new to start a new task."
            ),
        )
        return None
    except TaskCancellingError as exc:
        inbound_store.set_state(inbound_key, "failed")
        await safe_reply(
            message,
            t(
                "⏸ Task {task_id} is still cancelling. Wait for confirmation or ask an admin to force-cancel it."
            ).format(task_id=str(exc)),
        )
        return None
    except TaskStuckError as exc:
        inbound_store.set_state(inbound_key, "failed")
        await safe_reply(
            message,
            f"❌ 任务 `{exc}` 的提交状态异常。为避免串题，本条未发送。"
            f"请使用 `/task_retry {exc}` 或取消任务。",
        )
        return None
    except TaskQueueCancelledError:
        inbound_store.set_state(inbound_key, "failed")
        return None

    record_request(
        window_id,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        preserve_existing=admission.continuation,
    )
    logger.info(
        "Task request admitted",
        task_id=admission.task_id,
        member_id=user_id,
        topic_id=thread_id,
        window_id=window_id,
        chat_id=chat_id,
        continuation=admission.continuation,
        queued=admission.queued,
    )
    request_operations_dashboard_refresh(chat_id, thread_id)
    # Lazy: window_query reaches the runtime window-store adapter.
    from .. import window_query

    # Every terminal write, including a supplement to an active task, gets a
    # fresh submit-confirmation boundary. This prevents two missed supplements
    # from accumulating in the same TUI input buffer.
    dispatch_confirmation.begin(
        task_id=admission.task_id,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        window_id=window_id,
        provider=window_query.get_window_provider(window_id) or "unknown",
    )
    if admission.continuation:
        if queue_message is not None:
            await safe_edit(queue_message, t("➕ Added to your current task."))
        else:
            await safe_reply(message, t("➕ Added to your current task."))
    else:
        await publish_task_receipt(
            message,
            inbound_key=inbound_key,
            task_id=admission.task_id,
            existing=queue_message,
        )
    return admission


__all__ = ["admit_request", "message_ids"]
