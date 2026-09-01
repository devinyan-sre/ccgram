"""Telegram commands for inspecting and controlling multi-operator tasks."""

from __future__ import annotations

import asyncio
import re

from telegram import Update
from telegram.ext import ContextTypes

from ..access_control import has_role, role_for
from ..config import config
from ..dispatch_confirmation import dispatch_confirmation
from ..multiplexer import multiplexer as tmux_manager
from ..task_cancellation import CancellationResult, force_cancel, graceful_cancel
from ..task_focus import select as select_task_focus
from ..task_lanes import clear_task_lane, find_task_lane
from ..task_scheduler import task_scheduler
from .callback_helpers import get_thread_id
from .callback_registry import register
from .messaging_pipeline.message_sender import safe_edit, safe_reply
from .text.text_handler import handle_text_message
from .text.member_lanes import create_parallel_task_lane

_COMMAND_WITH_ARGUMENTS = 2
_TASK_CALLBACK_PREFIXES = ("tsk:focus:", "tsk:detail:", "tsk:cancel:")


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
                f"• `{view.task_id}` {owner}：已确认异常，通道已暂停，"
                f"使用 `/task_retry {view.task_id}` 或取消"
            )
        else:
            status = "可能停滞" if view.phase == "slow" else "正常执行"
            idle = max(0, int(view.idle_seconds))
            lines.append(
                f"• `{view.task_id}` {owner}：{status}，已运行 {age}s，"
                f"最近进展 {idle}s 前，"
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
        "ambiguous": "⚠️ 你有多个活动任务，请使用 `/task_cancel T编号` 明确指定。",
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
    if task_id is None:
        owned = [
            view
            for view in task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
            if view.user_id == user_id
        ]
        if len(owned) > 1:
            return CancellationResult("ambiguous")
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
    """``/task_add [Txxxx] text`` — supplement one exact active task."""
    if update.message is None:
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < _COMMAND_WITH_ARGUMENTS or not parts[1].strip():
        await safe_reply(
            update.message, "用法：`/task_add 补充内容` 或 `/task_add T0032 补充内容`"
        )
        return
    payload = parts[1].strip()
    target_task_id: str | None = None
    target_window_id: str | None = None
    task_match = re.match(r"^(T\d+)\s+(.+)$", payload, flags=re.IGNORECASE | re.DOTALL)
    if task_match:
        target_task_id = task_match.group(1).upper()
        payload = task_match.group(2).strip()
        scope = _scope(update)
        if scope is None:
            return
        chat_id, thread_id, user_id = scope
        view = next(
            (
                row
                for row in task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
                if row.user_id == user_id
                and row.task_id.upper() == target_task_id
                and row.state != "queued"
            ),
            None,
        )
        if view is None:
            await safe_reply(
                update.message,
                f"❌ 未找到你正在运行的任务 `{target_task_id}`，本条未发送。",
            )
            return
        target_window_id = view.window_id
    await handle_text_message(
        update,
        context,
        text_override=payload,
        target_window_id=target_window_id,
        target_task_id=target_task_id,
    )


async def task_parallel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_parallel text`` — explicitly create an isolated task lane."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < _COMMAND_WITH_ARGUMENTS or not parts[1].strip():
        await safe_reply(update.message, "用法：`/task_parallel 新问题内容`")
        return
    chat_id, thread_id, user_id = scope
    active = [
        row
        for row in task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
        if row.user_id == user_id and row.state != "queued"
    ]
    if len(active) >= config.max_parallel_per_operator:
        await safe_reply(
            update.message,
            "⏳ 你的并行任务已达到配置上限。可等待任务完成、取消任务，或调整 "
            "`CCGRAM_MAX_PARALLEL_PER_OPERATOR`。",
        )
        return
    task_id = await task_scheduler.reserve_task_id()
    creating = await safe_reply(
        update.message, f"🧩 正在为 `{task_id}` 创建独立 CLI 与工作区…"
    )
    lane = await create_parallel_task_lane(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        task_id=task_id,
    )
    if not lane.ready or lane.window_id is None:
        if creating is not None:
            await safe_edit(creating, f"❌ `{task_id}` 创建失败：{lane.error}")
        else:
            await safe_reply(update.message, f"❌ `{task_id}` 创建失败：{lane.error}")
        return
    if creating is not None:
        await safe_edit(creating, f"✅ `{task_id}` 独立通道已就绪，正在提交问题。")
    await handle_text_message(
        update,
        context,
        text_override=parts[1].strip(),
        target_window_id=lane.window_id,
        target_task_id=task_id,
    )


async def task_archive_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/task_archive Txxxx`` — stop a completed lane, retain Git results."""
    if update.message is None or (scope := _scope(update)) is None:
        return
    task_id = _argument(update)
    if task_id is None:
        await safe_reply(update.message, "用法：`/task_archive T0032`")
        return
    chat_id, thread_id, user_id = scope
    active = next(
        (
            row
            for row in task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
            if row.user_id == user_id and row.task_id.upper() == task_id
        ),
        None,
    )
    if active is not None:
        await safe_reply(
            update.message,
            f"⏳ 任务 `{task_id}` 仍在运行或排队，请先等待完成或取消。",
        )
        return
    window_id = find_task_lane(
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        task_id=task_id,
    )
    if window_id is None:
        await safe_reply(update.message, f"ℹ️ 未找到你的任务通道 `{task_id}`。")
        return
    live = await tmux_manager.find_window_by_id(window_id)
    if live is not None and not await tmux_manager.kill_window(window_id):
        await safe_reply(
            update.message, f"❌ 无法停止任务通道 `{task_id}`，状态保持不变。"
        )
        return
    clear_task_lane(window_id)
    await safe_reply(
        update.message,
        f"✅ 已归档任务通道 `{task_id}`。CLI 已停止；会话记录、分支和 worktree 均保留。",
    )


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


@register(*_TASK_CALLBACK_PREFIXES)
async def task_card_callback(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle task-card controls without embedding provider assumptions."""
    query = update.callback_query
    if query is None or not query.data or (scope := _scope(update)) is None:
        return
    try:
        _prefix, action, task_id = query.data.split(":", 2)
    except ValueError:
        await query.answer("无效的任务操作。", show_alert=True)
        return
    chat_id, thread_id, user_id = scope
    view = next(
        (
            row
            for row in task_scheduler.views(chat_id=chat_id, thread_id=thread_id)
            if row.task_id.upper() == task_id.upper()
            and (row.user_id == user_id or has_role(user_id, "admin"))
        ),
        None,
    )
    if action == "focus":
        if view is None or view.user_id != user_id:
            await query.answer("只能选择自己正在运行的任务。", show_alert=True)
            return
        select_task_focus(chat_id, thread_id, user_id, task_id)
        await query.answer(
            f"已选择 {task_id.upper()}。请在 {config.task_selection_ttl_seconds} 秒内发送下一条补充消息。",
            show_alert=True,
        )
        return
    if action == "detail":
        if view is None:
            await query.answer("任务已结束或不存在。", show_alert=True)
            return
        await query.answer(
            f"{view.task_id} · {view.state} · 阶段 {view.phase} · 补充 {view.supplements} 条",
            show_alert=True,
        )
        return
    if action == "cancel":
        result = await graceful_cancel(
            chat_id=chat_id,
            thread_id=thread_id,
            requester_user_id=user_id,
            task_id=task_id,
            allow_any=has_role(user_id, "admin"),
        )
        await query.answer(_cancel_text(result), show_alert=True)


__all__ = [
    "task_add_command",
    "task_archive_command",
    "task_cancel_all_command",
    "task_cancel_command",
    "task_force_cancel_command",
    "task_new_command",
    "task_parallel_command",
    "task_retry_command",
    "tasks_command",
]
