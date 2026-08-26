"""Telegram commands for inspecting and controlling multi-operator tasks."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ..access_control import has_role, role_for
from ..inbound_store import inbound_store
from ..multiplexer import multiplexer
from ..request_context import clear_window
from ..task_scheduler import task_scheduler
from .callback_helpers import get_thread_id
from .messaging_pipeline.message_sender import safe_reply
from .text.text_handler import handle_text_message

_COMMAND_WITH_ARGUMENTS = 2


def _scope(update: Update) -> tuple[int, int, int] | None:
    user = update.effective_user
    chat = update.effective_chat
    thread_id = get_thread_id(update)
    if user is None or chat is None or thread_id is None:
        return None
    return chat.id, thread_id, user.id


async def tasks_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/tasks`` — show active and queued work in the current topic."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    chat_id, thread_id, user_id = scope
    views = task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
    if not views:
        await safe_reply(update.message, "✅ 当前话题没有运行中或排队任务。")
        return
    admin = role_for(user_id) == "admin"
    lines = ["📋 当前话题任务："]
    for view in views:
        owner = (
            "你"
            if view.user_id == user_id
            else (str(view.user_id) if admin else "其他成员")
        )
        age = max(0, int(view.age_seconds))
        if view.state == "queued":
            lines.append(f"• {owner}：排队 #{view.queue_position}，等待 {age}s")
        else:
            lines.append(
                f"• {owner}：运行中 {age}s，补充 {view.supplements} 条，"
                f"窗口 `{view.window_id}`"
            )
    await safe_reply(update.message, "\n".join(lines))


async def _cancel(scope: tuple[int, int, int]) -> str:
    chat_id, thread_id, user_id = scope
    window_id = await task_scheduler.cancel_operator(
        chat_id=chat_id, thread_id=thread_id, user_id=user_id
    )
    if window_id is None:
        return "ℹ️ 你当前没有运行中或排队任务。"
    if not window_id:
        return "✅ 已取消你的排队任务。"
    await multiplexer.send_keys(window_id, "C-c", enter=False, literal=False)
    inbound_store.mark_window_done(window_id, failed=True)
    clear_window(window_id)
    return "✅ 已中断并取消你的当前任务。"


async def task_cancel_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_cancel`` — cancel only the caller's task."""
    if update.message is not None and (scope := _scope(update)) is not None:
        await safe_reply(update.message, await _cancel(scope))


async def task_new_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/task_new`` — terminate the current task and establish a boundary."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    result = await _cancel(scope)
    await safe_reply(
        update.message,
        result + "\n🆕 下一条普通消息会作为一个新任务开始。",
    )


async def task_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/task_add text`` — explicitly supplement the caller's active task."""
    if update.message is None:
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < _COMMAND_WITH_ARGUMENTS or not parts[1].strip():
        await safe_reply(update.message, "用法：`/task_add 补充内容`")
        return
    await handle_text_message(update, context, text_override=parts[1].strip())


async def task_cancel_all_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_cancel_all`` — admin-only cancellation for the current topic."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    chat_id, thread_id, user_id = scope
    if not has_role(user_id, "admin"):
        await safe_reply(update.message, "❌ 只有管理员可以取消其他成员的任务。")
        return
    views = task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
    cancelled = 0
    for view in views:
        window_id = await task_scheduler.cancel_operator(
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=view.user_id,
        )
        if window_id:
            await multiplexer.send_keys(window_id, "C-c", enter=False, literal=False)
            inbound_store.mark_window_done(window_id, failed=True)
            clear_window(window_id)
        if window_id is not None:
            cancelled += 1
    await safe_reply(update.message, f"✅ 已取消本话题 {cancelled} 个任务。")


__all__ = [
    "task_add_command",
    "task_cancel_all_command",
    "task_cancel_command",
    "task_new_command",
    "tasks_command",
]
