"""Inspect, archive, restore and safely clean an operator's isolated lane."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from .. import window_query
from ..access_control import has_role
from ..multiplexer import multiplexer
from ..task_scheduler import task_scheduler
from ..thread_router import thread_router
from ..window_state_ports import lifecycle_state, worktree_state
from ..worktree_cleanup import cleanup_merged_worktree
from ..worktree_coordination import topic_conflicts
from .callback_helpers import get_thread_id
from .lifecycle_commands import park_command, wake_command
from .messaging_pipeline.message_sender import safe_reply


async def lane_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/lane [status|archive|restore|cleanup]`` — manage the caller's lane."""
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    thread_id = get_thread_id(update)
    if message is None or user is None or chat is None or thread_id is None:
        return
    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if window_id is None:
        await safe_reply(message, "❌ 当前成员在这个话题中没有绑定工作区。")
        return
    args = (message.text or "").split()
    action = args[1].lower() if len(args) > 1 else "status"
    if action != "status" and not has_role(user.id, "operator"):
        await safe_reply(message, "❌ 只读成员不能修改工作区状态。")
        return
    if action in ("archive", "park"):
        await park_command(update, context)
        return
    if action in ("restore", "wake"):
        await wake_command(update, context)
        return
    if action == "cleanup":
        await _cleanup(update, window_id, user.id, thread_id)
        return
    if action != "status":
        await safe_reply(message, "用法：`/lane [status|archive|restore|cleanup]`")
        return

    view = window_query.view_window(window_id)
    worktree = worktree_state.get_worktree(window_id)
    report = await topic_conflicts(
        chat_id=chat.id, thread_id=thread_id, window_id=window_id
    )
    lifecycle = "已挂起" if lifecycle_state.is_parked(window_id) else "运行中"
    conflicts = (
        "、".join(f"`{path}`" for path in report.files[:8]) if report.files else "无"
    )
    await safe_reply(
        message,
        "\n".join(
            [
                f"🧭 成员工作区：`{window_id}`",
                f"状态：{lifecycle}",
                f"CLI：{view.provider_name if view else 'unknown'}",
                f"目录：`{view.cwd if view else ''}`",
                f"分支：`{worktree.worktree_branch if worktree else ''}`",
                f"与其他成员重叠文件：{conflicts}",
            ]
        ),
    )


async def _cleanup(
    update: Update, window_id: str, user_id: int, thread_id: int
) -> None:
    message = update.message
    assert message is not None
    if not thread_router.is_member_lane(window_id):
        await safe_reply(message, "❌ 主工作区不能通过 `/lane cleanup` 删除。")
        return
    if await multiplexer.find_window_by_id(window_id) is not None:
        await safe_reply(
            message, "❌ 请先执行 `/lane archive` 挂起 CLI，再清理工作区。"
        )
        return
    worktree = worktree_state.get_worktree(window_id)
    if worktree is None or not worktree.worktree_path or not worktree.worktree_branch:
        await safe_reply(message, "❌ 没有可安全清理的 worktree 元数据。")
        return
    result = await asyncio.to_thread(
        cleanup_merged_worktree,
        Path(worktree.worktree_path),
        worktree.worktree_branch,
    )
    if not result.removed:
        await safe_reply(message, "❌ " + result.reason)
        return
    await task_scheduler.cancel_operator(
        chat_id=message.chat.id, thread_id=thread_id, user_id=user_id
    )
    thread_router.unbind_thread(user_id, thread_id)
    thread_router.clear_member_lane(window_id)
    worktree_state.clear_worktree(window_id)
    await safe_reply(message, "✅ " + result.reason)


__all__ = ["lane_command"]
