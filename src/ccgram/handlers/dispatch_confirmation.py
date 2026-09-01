"""Inline controls for a task whose provider submit was not confirmed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update

from ..access_control import has_role
from ..dispatch_confirmation import (
    CB_DISPATCH_CANCEL_PREFIX,
    CB_DISPATCH_RETRY_PREFIX,
    dispatch_confirmation,
)
from ..task_cancellation import graceful_cancel
from .callback_helpers import get_thread_id
from .callback_registry import register

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


@register(CB_DISPATCH_RETRY_PREFIX)
async def handle_dispatch_retry(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or not query.data:
        return
    task_id = query.data.removeprefix(CB_DISPATCH_RETRY_PREFIX).upper()
    result = await dispatch_confirmation.retry(task_id, requester_user_id=user.id)
    texts = {
        "retrying": "已重新提交，正在等待 CLI 确认。",
        "not_found": "任务不存在或已经结束。",
        "forbidden": "只能重新提交自己的任务。",
        "not_stuck": "任务当前不需要重新提交。",
        "failed": "提交键发送失败，通道仍保持暂停。",
    }
    await query.answer(texts.get(result, result), show_alert=result != "retrying")


@register(CB_DISPATCH_CANCEL_PREFIX)
async def handle_dispatch_cancel(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    thread_id = get_thread_id(update)
    if (
        query is None
        or user is None
        or chat is None
        or thread_id is None
        or not query.data
    ):
        return
    task_id = query.data.removeprefix(CB_DISPATCH_CANCEL_PREFIX).upper()
    result = await graceful_cancel(
        chat_id=chat.id,
        thread_id=thread_id,
        requester_user_id=user.id,
        task_id=task_id,
        allow_any=has_role(user.id, "admin"),
    )
    await query.answer(
        "任务已取消。"
        if result.status == "cancel_confirmed"
        else f"取消状态：{result.status}",
        show_alert=result.status != "cancel_confirmed",
    )


__all__ = ["handle_dispatch_cancel", "handle_dispatch_retry"]
