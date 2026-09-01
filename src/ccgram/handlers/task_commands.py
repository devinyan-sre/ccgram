"""Telegram commands for inspecting and controlling multi-operator tasks."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ..access_control import has_role, role_for
from ..dispatch_confirmation import dispatch_confirmation
from ..task_cancellation import CancellationResult, force_cancel, graceful_cancel
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


def _argument(update: Update) -> str | None:
    if update.message is None:
        return None
    parts = (update.message.text or "").split(maxsplit=1)
    return parts[1].strip().upper() if len(parts) > 1 and parts[1].strip() else None


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
            eta = view.estimated_wait_seconds or 0
            lines.append(
                f"• `{view.task_id}` {owner}：排队 #{view.queue_position}，"
                f"等待 {age}s，预计 ≤{eta}s，补充 {view.supplements} 条"
            )
        elif view.state == "cancelling":
            lines.append(
                f"• `{view.task_id}` {owner}：取消确认中 {age}s，"
                f"窗口 `{view.window_id}`"
            )
        elif view.state == "stuck":
            lines.append(
                f"• `{view.task_id}` {owner}：CLI 未确认启动，通道已暂停，"
                f"使用 `/task_retry {view.task_id}` 或取消"
            )
        else:
            lines.append(
                f"• `{view.task_id}` {owner}：运行中 {age}s，"
                f"补充 {view.supplements} 条，窗口 `{view.window_id}`"
            )
    await safe_reply(update.message, "\n".join(lines))


def _cancel_text(result: CancellationResult) -> str:
    simple = {
        "not_found": "ℹ️ 没有找到可取消的任务。",
        "forbidden": "❌ 只能取消自己的任务；管理员可以按任务编号取消其他任务。",
        "queued_cancelled": f"✅ 已取消排队任务 `{result.task_id}`。",
        "queued_force_cancelled": f"✅ 已取消排队任务 `{result.task_id}`。",
        "cancel_confirmed": (
            f"✅ CLI 已确认停止，任务 `{result.task_id}` 已取消并释放槽位。"
        ),
    }
    if result.status in simple:
        return simple[result.status]
    if result.status == "interrupt_failed":
        return (
            f"⚠️ 无法向任务 `{result.task_id}` 发送中断，任务仍占用槽位。"
            "管理员可使用 `/task_force_cancel "
            f"{result.task_id}`。"
        )
    if result.status == "cancel_timeout":
        return (
            f"⚠️ 已向任务 `{result.task_id}` 发送 Ctrl+C，但在确认时限内未观察到"
            " CLI 停止；任务保持“取消中”且不会与新任务重叠。管理员可使用 "
            f"`/task_force_cancel {result.task_id}`。"
        )
    if result.status == "force_cancelled":
        return (
            f"✅ 已强制终止任务 `{result.task_id}` 的 CLI。话题、历史和工作区仍保留，"
            "下一条消息会进入恢复流程。"
        )
    if result.status == "force_cancel_failed":
        return f"❌ 强制终止任务 `{result.task_id}` 失败，未释放任务槽位。"
    return f"ℹ️ 任务状态：{result.status}"


async def _cancel(update: Update) -> CancellationResult | None:
    scope = _scope(update)
    if scope is None:
        return None
    chat_id, thread_id, user_id = scope
    task_id = _argument(update)
    return await graceful_cancel(
        chat_id=chat_id,
        thread_id=thread_id,
        requester_user_id=user_id,
        task_id=task_id,
        allow_any=has_role(user_id, "admin"),
    )


async def task_cancel_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_cancel [Txxxx]`` — gracefully cancel an owned/specified task."""
    if update.message is not None and (result := await _cancel(update)) is not None:
        await safe_reply(update.message, _cancel_text(result))


async def task_new_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/task_new`` — terminate the current task and establish a boundary."""
    if update.message is None or (result := await _cancel(update)) is None:
        return
    suffix = (
        "\n🆕 下一条普通消息会作为新任务开始。"
        if result.status in ("not_found", "queued_cancelled", "cancel_confirmed")
        else "\n⏸ 请等待取消确认，或由管理员强制取消后再开始新任务。"
    )
    await safe_reply(update.message, _cancel_text(result) + suffix)


async def task_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/task_add text`` — explicitly supplement the caller's active task."""
    if update.message is None:
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < _COMMAND_WITH_ARGUMENTS or not parts[1].strip():
        await safe_reply(update.message, "用法：`/task_add 补充内容`")
        return
    await handle_text_message(update, context, text_override=parts[1].strip())


async def task_retry_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_retry [Txxxx]`` — retry only the submit key, never the prompt."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    chat_id, thread_id, user_id = scope
    task_id = _argument(update)
    if task_id is None:
        record = next(
            (
                row
                for row in dispatch_confirmation.records()
                if row.chat_id == chat_id
                and row.thread_id == thread_id
                and row.user_id == user_id
                and row.status in ("submitting", "stuck")
            ),
            None,
        )
        task_id = record.task_id if record is not None else None
    if task_id is None:
        await safe_reply(update.message, "ℹ️ 没有需要重新提交的任务。")
        return
    result = await dispatch_confirmation.retry(task_id, requester_user_id=user_id)
    texts = {
        "retrying": f"⌨️ 已重新提交任务 `{task_id}`，正在等待 CLI 确认。",
        "not_found": f"ℹ️ 未找到任务 `{task_id}`。",
        "forbidden": "❌ 只能重新提交自己的任务。",
        "not_stuck": f"ℹ️ 任务 `{task_id}` 当前不需要重新提交。",
        "failed": f"❌ 任务 `{task_id}` 的提交键发送失败，通道仍保持暂停。",
    }
    await safe_reply(update.message, texts.get(result, f"ℹ️ 状态：{result}"))


async def task_force_cancel_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_force_cancel Txxxx`` — admin-only hard stop preserving state."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    task_id = _argument(update)
    if task_id is None:
        await safe_reply(update.message, "用法：`/task_force_cancel T0001`")
        return
    chat_id, thread_id, user_id = scope
    result = await force_cancel(
        chat_id=chat_id,
        thread_id=thread_id,
        requester_user_id=user_id,
        task_id=task_id,
    )
    await safe_reply(update.message, _cancel_text(result))


async def task_cancel_all_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_cancel_all`` — gracefully cancel all tasks in this topic."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    chat_id, thread_id, user_id = scope
    if not has_role(user_id, "admin"):
        await safe_reply(update.message, "❌ 只有管理员可以取消其他成员的任务。")
        return
    task_ids = [
        view.task_id
        for view in task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
    ]
    results = await asyncio.gather(
        *(
            graceful_cancel(
                chat_id=chat_id,
                thread_id=thread_id,
                requester_user_id=user_id,
                task_id=task_id,
                allow_any=True,
            )
            for task_id in task_ids
        )
    )
    confirmed = sum(
        result.status in ("queued_cancelled", "cancel_confirmed") for result in results
    )
    pending = sum(result.status == "cancel_timeout" for result in results)
    failed = len(results) - confirmed - pending
    await safe_reply(
        update.message,
        f"✅ 已处理 {len(results)} 个任务：确认取消 {confirmed}，"
        f"仍在取消中 {pending}，失败 {failed}。",
    )


__all__ = [
    "task_add_command",
    "task_cancel_all_command",
    "task_cancel_command",
    "task_force_cancel_command",
    "task_new_command",
    "task_retry_command",
    "tasks_command",
]
