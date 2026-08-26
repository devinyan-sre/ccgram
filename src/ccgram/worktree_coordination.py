"""Detect overlapping file edits across isolated member worktrees."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from . import window_query
from .telegram_client import TelegramClient
from .thread_router import thread_router

logger = structlog.get_logger()
_last_warning: dict[str, tuple[str, ...]] = {}
_PORCELAIN_PREFIX = 3
_CONFLICT_PREVIEW_LIMIT = 12


@dataclass(frozen=True, slots=True)
class ConflictReport:
    files: tuple[str, ...]
    other_windows: tuple[str, ...]


async def _git_lines(cwd: str, *args: str) -> list[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return []
        return [line.strip() for line in stdout.decode().splitlines() if line.strip()]
    except OSError, TimeoutError:
        return []


async def changed_files(cwd: str) -> set[str]:
    """Return committed and uncommitted paths changed on one lane branch."""
    status = await _git_lines(cwd, "status", "--porcelain")
    files = {
        line[_PORCELAIN_PREFIX:].split(" -> ")[-1]
        for line in status
        if len(line) > _PORCELAIN_PREFIX and line[_PORCELAIN_PREFIX:].strip()
    }
    # Member branches use the ``ccg/`` namespace. The fork point with the
    # first parent is not persisted, so compare against the merge-base with
    # the repository's default remote branch when available.
    bases = await _git_lines(cwd, "rev-parse", "--verify", "refs/remotes/origin/HEAD")
    base = "origin/HEAD" if bases else "HEAD~0"
    merge_bases = await _git_lines(cwd, "merge-base", "HEAD", base)
    if merge_bases:
        files.update(
            await _git_lines(cwd, "diff", "--name-only", f"{merge_bases[0]}..HEAD")
        )
    return files


async def topic_conflicts(
    *, chat_id: int, thread_id: int, window_id: str
) -> ConflictReport:
    view = window_query.view_window(window_id)
    if view is None or not view.cwd:
        return ConflictReport((), ())
    own = await changed_files(view.cwd)
    if not own:
        return ConflictReport((), ())
    overlaps: set[str] = set()
    other_windows: set[str] = set()
    for _owner, other_id in thread_router.get_bindings_for_chat_thread(
        chat_id, thread_id
    ):
        if other_id == window_id:
            continue
        other = window_query.view_window(other_id)
        if other is None or not other.cwd:
            continue
        common = own & await changed_files(other.cwd)
        if common:
            overlaps.update(common)
            other_windows.add(other_id)
    return ConflictReport(tuple(sorted(overlaps)), tuple(sorted(other_windows)))


async def warn_topic_conflicts(
    client: TelegramClient,
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
) -> ConflictReport:
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    report = await topic_conflicts(
        chat_id=chat_id, thread_id=thread_id, window_id=window_id
    )
    if not report.files or _last_warning.get(window_id) == report.files:
        return report
    _last_warning[window_id] = report.files
    preview = "\n".join(
        f"• `{path}`" for path in report.files[:_CONFLICT_PREVIEW_LIMIT]
    )
    suffix = "\n• …" if len(report.files) > _CONFLICT_PREVIEW_LIMIT else ""
    await client.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=(
            "⚠️ 检测到其他成员工作区也修改了相同文件。当前工作区仍然隔离，"
            "但合并时可能冲突：\n" + preview + suffix
        ),
    )
    logger.warning(
        "Member worktree file overlap",
        window_id=window_id,
        files=list(report.files),
        other_windows=list(report.other_windows),
    )
    return report


def reset_for_testing() -> None:
    _last_warning.clear()


__all__ = [
    "ConflictReport",
    "changed_files",
    "reset_for_testing",
    "topic_conflicts",
    "warn_topic_conflicts",
]
