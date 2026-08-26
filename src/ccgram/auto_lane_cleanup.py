"""Retention-based, fail-closed cleanup of archived member worktrees."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time

import structlog

from .config import config
from .destructive_audit import (
    ACTION_MEMBER_WORKTREE_CLEANED,
    ACTOR_AUTO,
    OUTCOME_SKIPPED_DRYRUN,
    record_destructive,
)
from .telegram_client import TelegramClient
from .thread_router import thread_router
from .window_state_ports import lifecycle_state, pane_state, worktree_state
from .worktree_cleanup import cleanup_merged_worktree

logger = structlog.get_logger()


async def check_auto_lane_cleanup(client: TelegramClient) -> int:
    """Remove only parked, clean, merged member worktrees past retention."""
    if config.member_lane_cleanup_days <= 0:
        return 0
    cutoff = time.time() - config.member_lane_cleanup_days * 86400
    cleaned = 0
    for user_id, thread_id, window_id in list(thread_router.iter_thread_bindings()):
        if not thread_router.is_member_lane(window_id) or not lifecycle_state.is_parked(
            window_id
        ):
            continue
        worktree = worktree_state.get_worktree(window_id)
        if (
            worktree is None
            or not worktree.worktree_path
            or not worktree.worktree_branch
        ):
            continue
        panes = pane_state.list_pane_projections(window_id)
        last_active = max((pane.last_active_ts for pane in panes), default=0.0)
        if not last_active:
            try:
                last_active = Path(worktree.worktree_path).stat().st_mtime
            except OSError:
                last_active = 0.0
        if not last_active or last_active > cutoff:
            continue
        if config.destructive_dryrun:
            await record_destructive(
                ACTION_MEMBER_WORKTREE_CLEANED,
                actor=ACTOR_AUTO,
                window_id=window_id,
                thread_id=thread_id,
                user_id=user_id,
                outcome=OUTCOME_SKIPPED_DRYRUN,
                detail="eligible clean/merged worktree retained by dry-run mode",
            )
            continue
        result = await asyncio.to_thread(
            cleanup_merged_worktree,
            Path(worktree.worktree_path),
            worktree.worktree_branch,
        )
        if not result.removed:
            logger.info(
                "Member lane not eligible for automatic cleanup",
                window_id=window_id,
                reason=result.reason,
            )
            continue
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        thread_router.unbind_thread(user_id, thread_id)
        thread_router.clear_member_lane(window_id)
        worktree_state.clear_worktree(window_id)
        await client.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=("🧹 已按保留策略清理一个超过期限、干净且已合并的成员工作区。"),
        )
        await record_destructive(
            ACTION_MEMBER_WORKTREE_CLEANED,
            actor=ACTOR_AUTO,
            window_id=window_id,
            thread_id=thread_id,
            user_id=user_id,
            detail=result.reason,
        )
        cleaned += 1
    return cleaned


__all__ = ["check_auto_lane_cleanup"]
