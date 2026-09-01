"""Provision one isolated provider lane per allow-listed topic member."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog

from ... import window_query
from ...config import config
from ...multiplexer import multiplexer as tmux_manager
from ...metrics import MEMBER_LANE_CREATE_FAILED
from ...providers import get_provider_for_window, resolve_launch_command
from ...provider_readiness import wait_for_provider_ready
from ...session import session_manager
from ...thread_router import thread_router
from ...topic_naming import reserve_topic_name
from ...window_state_store import CCGRAM_CREATED_WINDOW_ORIGIN
from ..topics import topic_orchestration
from ..topics.worktree import (
    WorktreeError,
    check_worktree_eligibility,
    create_worktree,
    primary_worktree_path,
    slug_for_path,
    suggest_branch_name,
    worktree_path_for,
)

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class LaneResult:
    handled: bool
    ready: bool
    window_id: str | None = None
    error: str = ""
    worktree_path: str = ""
    worktree_branch: str = ""


_lane_locks: dict[tuple[int | str, ...], asyncio.Lock] = {}
_creation_slots: asyncio.Semaphore | None = None


def _lane_failure(reason: str, error: str) -> LaneResult:
    MEMBER_LANE_CREATE_FAILED.inc(reason=reason)
    return LaneResult(handled=True, ready=False, error=error)


def _creation_semaphore() -> asyncio.Semaphore:
    global _creation_slots
    if _creation_slots is None:
        # Window creation briefly invokes several subprocesses. Bound it below
        # Telegram update concurrency to avoid fork storms during shift change.
        _creation_slots = asyncio.Semaphore(config.max_parallel_global)
    return _creation_slots


async def _find_template(chat_id: int, thread_id: int) -> tuple[str, str, str] | None:
    """Return ``(window_id, cwd, provider)`` from a live topic lane."""
    bindings = thread_router.get_bindings_for_chat_thread(chat_id, thread_id)
    workspace_window = thread_router.get_workspace_window_for_chat_thread(
        chat_id, thread_id
    )
    ordered = sorted(
        bindings,
        key=lambda binding: binding[1] != workspace_window,
    )
    for _owner_id, window_id in ordered:
        view = window_query.view_window(window_id)
        if view is None or not view.cwd or not view.provider_name:
            continue
        live = await tmux_manager.find_window_by_id(window_id)
        if live is not None:
            return window_id, view.cwd, view.provider_name
    return None


async def _isolated_cwd(
    cwd: str, *, user_id: int, thread_id: int
) -> tuple[str, str, str] | LaneResult:
    """Create a per-member git worktree or fail closed for unsafe sharing."""
    if not config.member_lane_worktrees:
        if config.allow_shared_member_cwd:
            return cwd, "", ""
        return _lane_failure(
            "isolation_disabled",
            (
                "Concurrent lanes require Git worktree isolation. Enable "
                "CCGRAM_MEMBER_LANE_WORKTREES or explicitly allow a shared cwd."
            ),
        )

    eligibility = await asyncio.to_thread(check_worktree_eligibility, Path(cwd))
    if not eligibility.eligible or eligibility.repo_path is None:
        if config.allow_shared_member_cwd:
            return cwd, "", ""
        return _lane_failure(
            "ineligible_worktree",
            (
                "This workspace is not eligible for an isolated Git worktree; "
                "parallel access was blocked to prevent file collisions."
            ),
        )
    if eligibility.dirty:
        return _lane_failure(
            "dirty_workspace",
            (
                "The topic workspace has uncommitted changes. Commit or stash "
                "them before another member starts a parallel lane."
            ),
        )

    repo = eligibility.repo_path
    relative = Path(cwd).resolve().relative_to(repo.resolve())
    branch = await asyncio.to_thread(
        suggest_branch_name,
        f"member-{thread_id}-{user_id}",
        repo,
    )
    path = worktree_path_for(repo, slug_for_path(branch))
    try:
        await asyncio.to_thread(create_worktree, repo, branch, path)
    except WorktreeError as exc:
        return _lane_failure("worktree_create", str(exc))
    target = path / relative
    return str(target if target.is_dir() else path), str(path), branch


async def ensure_member_lane(  # noqa: C901, PLR0911 - fail-closed stages
    *, user_id: int, chat_id: int, thread_id: int
) -> LaneResult:
    """Create an independent CLI window for a new member of an existing topic.

    Correctness never depends on provider-native sub-agents: Claude, Codex,
    Gemini, Pi and shell all use the same multiplexer/provider launch seam.
    Derived member lanes always use normal approval mode; inheriting a YOLO
    lane into several concurrent operators would multiply write risk.
    """
    if not config.member_lanes_enabled:
        return LaneResult(handled=False, ready=False)
    if thread_router.get_window_for_thread(user_id, thread_id) is not None:
        return LaneResult(handled=True, ready=True)

    key = (chat_id, thread_id, user_id)
    lock = _lane_locks.setdefault(key, asyncio.Lock())
    async with lock:
        existing = thread_router.get_window_for_thread(user_id, thread_id)
        if existing is not None:
            return LaneResult(handled=True, ready=True, window_id=existing)

        topic_lanes = thread_router.get_bindings_for_chat_thread(chat_id, thread_id)
        if not topic_lanes:
            return LaneResult(handled=False, ready=False)
        if len(topic_lanes) >= config.max_member_lanes_per_topic:
            return _lane_failure(
                "lane_limit",
                (
                    "This topic has reached its concurrent member limit "
                    f"({config.max_member_lanes_per_topic})."
                ),
            )

        template = await _find_template(chat_id, thread_id)
        if template is None:
            return _lane_failure(
                "no_template", "No live workspace lane is available in this topic."
            )
        _source_window, template_cwd, provider_name = template
        isolated = await _isolated_cwd(
            template_cwd,
            user_id=user_id,
            thread_id=thread_id,
        )
        if isinstance(isolated, LaneResult):
            return isolated
        cwd, worktree_path, worktree_branch = isolated
        provider = get_provider_for_window(_source_window, provider_name=provider_name)
        # Security boundary: never copy permissive/YOLO approval to another
        # human automatically. Each derived lane starts in normal mode.
        approval_mode = "normal"
        launch_command = resolve_launch_command(
            provider.capabilities.name, approval_mode=approval_mode
        )

        async with (
            _creation_semaphore(),
            reserve_topic_name(template_cwd, provider.capabilities.name) as name,
        ):
            (
                success,
                message,
                created_name,
                created_id,
            ) = await tmux_manager.create_window(
                cwd,
                window_name=name.name,
                launch_command=launch_command,
            )
            if not success or not created_id:
                logger.error(
                    "Failed to create member lane",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    provider=provider.capabilities.name,
                    error=message,
                )
                return _lane_failure("window_create", message)

            # Same race invariant as the normal topic creation flow: do
            # not await between create_window and this marker/binding.
            topic_orchestration.register_pending_creation(created_id)
            session_manager.set_window_origin(created_id, CCGRAM_CREATED_WINDOW_ORIGIN)
            session_manager.set_window_cwd(created_id, cwd)
            session_manager.set_window_provider(created_id, provider.capabilities.name)
            session_manager.set_window_approval_mode(created_id, approval_mode)
            session_manager.set_window_auto_named(created_id, value=name.automatic)
            session_manager.set_display_name(created_id, created_name)
            if worktree_path and worktree_branch:
                session_manager.set_window_worktree(
                    created_id, worktree_path, worktree_branch
                )
            thread_router.bind_thread(
                user_id,
                thread_id,
                created_id,
                window_name=created_name,
            )
            thread_router.set_group_chat_id(user_id, thread_id, chat_id)
            thread_router.mark_member_lane(created_id, user_id)
            topic_orchestration.clear_pending_creation(created_id)

        await tmux_manager.stamp_pane_title(created_id, provider.capabilities.name)
        readiness = await wait_for_provider_ready(
            created_id,
            provider.capabilities.name,
            timeout=20.0,
        )
        if not readiness.ready:
            logger.warning(
                "Member lane created but provider is not ready",
                user_id=user_id,
                window_id=created_id,
                provider=provider.capabilities.name,
                reason=readiness.reason,
            )
            MEMBER_LANE_CREATE_FAILED.inc(reason="provider_not_ready")
            return LaneResult(
                handled=True,
                ready=False,
                window_id=created_id,
                error=(
                    f"{provider.capabilities.name} lane was created but is not ready: "
                    f"{readiness.reason}"
                ),
            )

        logger.info(
            "Provisioned isolated member lane",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=created_id,
            provider=provider.capabilities.name,
            cwd=cwd,
        )
        return LaneResult(handled=True, ready=True, window_id=created_id)


async def create_parallel_task_lane(  # noqa: C901, PLR0911 - fail-closed stages
    *, user_id: int, chat_id: int, thread_id: int, task_id: str
) -> LaneResult:
    """Provision one provider-neutral CLI/worktree for an explicit task.

    The caller's default topic lane is the template. A task lane never changes
    the default Telegram binding and never inherits permissive approval mode.
    """
    default_window = thread_router.get_window_for_thread(user_id, thread_id)
    if default_window is None:
        return _lane_failure("no_default_lane", "当前成员在此话题还没有默认工作区。")
    lanes = thread_router.task_lanes_for_operator(
        chat_id=chat_id, thread_id=thread_id, user_id=user_id
    )
    if len(lanes) >= config.max_task_lanes_per_operator:
        return _lane_failure(
            "task_lane_limit",
            f"你在此话题的任务通道已达到上限（{config.max_task_lanes_per_operator}）。",
        )
    view = window_query.view_window(default_window)
    if view is None or not view.cwd or not view.provider_name:
        return _lane_failure("no_template", "默认工作区缺少目录或工具信息。")

    lock_key = (chat_id, thread_id, user_id, task_id)
    # Keep a separate lock namespace without weakening the existing member key.
    lock = _lane_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        existing = thread_router.task_lane_for_id(
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            task_id=task_id,
        )
        if existing:
            return LaneResult(handled=True, ready=True, window_id=existing)

        cwd = view.cwd
        worktree_path = ""
        worktree_branch = ""
        if config.parallel_task_worktrees:
            eligibility = await asyncio.to_thread(check_worktree_eligibility, Path(cwd))
            if not eligibility.eligible or eligibility.repo_path is None:
                return _lane_failure(
                    "task_worktree_ineligible",
                    "当前目录不能安全创建 Git worktree；并行任务已阻止，避免文件互相覆盖。",
                )
            if eligibility.dirty:
                return _lane_failure(
                    "task_workspace_dirty",
                    "默认工作区存在未提交改动。请先提交或暂存，再创建并行任务。",
                )
            source_repo = eligibility.repo_path
            relative = Path(cwd).resolve().relative_to(source_repo.resolve())
            primary_repo = await asyncio.to_thread(primary_worktree_path, source_repo)
            branch = await asyncio.to_thread(
                suggest_branch_name,
                f"task-{thread_id}-{user_id}-{task_id.lower()}",
                primary_repo,
            )
            path = worktree_path_for(primary_repo, slug_for_path(branch))
            try:
                # Run from the source worktree so HEAD matches the caller's
                # actual branch, even when that default lane is itself derived.
                await asyncio.to_thread(create_worktree, source_repo, branch, path)
            except WorktreeError as exc:
                return _lane_failure("task_worktree_create", str(exc))
            target = path / relative
            cwd = str(target if target.is_dir() else path)
            worktree_path, worktree_branch = str(path), branch
        elif not config.allow_shared_member_cwd:
            return _lane_failure(
                "task_isolation_disabled",
                "并行任务需要 worktree 隔离；请启用 CCGRAM_PARALLEL_TASK_WORKTREES。",
            )

        provider = get_provider_for_window(
            default_window, provider_name=view.provider_name
        )
        approval_mode = "normal"
        launch_command = resolve_launch_command(
            provider.capabilities.name, approval_mode=approval_mode
        )
        async with (
            _creation_semaphore(),
            reserve_topic_name(cwd, provider.capabilities.name) as name,
        ):
            success, error, created_name, created_id = await tmux_manager.create_window(
                cwd, window_name=name.name, launch_command=launch_command
            )
            if not success or not created_id:
                return _lane_failure("task_window_create", error)
            topic_orchestration.register_pending_creation(created_id)
            session_manager.set_window_origin(created_id, CCGRAM_CREATED_WINDOW_ORIGIN)
            session_manager.set_window_cwd(created_id, cwd)
            session_manager.set_window_provider(created_id, provider.capabilities.name)
            session_manager.set_window_approval_mode(created_id, approval_mode)
            session_manager.set_window_auto_named(created_id, value=name.automatic)
            session_manager.set_display_name(created_id, created_name)
            if worktree_path:
                session_manager.set_window_worktree(
                    created_id, worktree_path, worktree_branch
                )
            thread_router.register_task_lane(
                created_id,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                task_id=task_id,
            )
            topic_orchestration.clear_pending_creation(created_id)

        await tmux_manager.stamp_pane_title(created_id, provider.capabilities.name)
        readiness = await wait_for_provider_ready(
            created_id, provider.capabilities.name, timeout=20.0
        )
        if not readiness.ready:
            return _lane_failure(
                "task_provider_not_ready",
                f"{provider.capabilities.name} 通道已创建但尚未就绪：{readiness.reason}",
            )
        logger.info(
            "Provisioned isolated parallel task lane",
            task_id=task_id,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=created_id,
            provider=provider.capabilities.name,
            cwd=cwd,
        )
        return LaneResult(
            handled=True,
            ready=True,
            window_id=created_id,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
        )


def reset_for_testing() -> None:
    global _creation_slots
    _lane_locks.clear()
    _creation_slots = None


__all__ = [
    "LaneResult",
    "create_parallel_task_lane",
    "ensure_member_lane",
    "reset_for_testing",
]
