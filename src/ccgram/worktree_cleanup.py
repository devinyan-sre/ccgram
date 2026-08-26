"""Fail-closed cleanup for archived member-lane Git worktrees."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True, slots=True)
class CleanupResult:
    safe: bool
    removed: bool
    reason: str


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def cleanup_merged_worktree(path: Path, branch: str) -> CleanupResult:
    """Remove only a clean worktree whose branch is merged into the main HEAD."""
    if not path.is_dir():
        return CleanupResult(True, False, "工作区目录已经不存在。")
    status = _git(path, "status", "--porcelain")
    if status.returncode != 0:
        return CleanupResult(False, False, "无法读取 Git 工作区状态。")
    if status.stdout.strip():
        return CleanupResult(False, False, "工作区有未提交修改，已拒绝清理。")
    listing = _git(path, "worktree", "list", "--porcelain")
    main = next(
        (
            Path(line.removeprefix("worktree "))
            for line in listing.stdout.splitlines()
            if line.startswith("worktree ")
        ),
        None,
    )
    if listing.returncode != 0 or main is None:
        return CleanupResult(False, False, "无法定位主工作区。")
    merged = _git(main, "branch", "--merged", "HEAD", "--format=%(refname:short)")
    merged_names = {line.strip() for line in merged.stdout.splitlines()}
    if merged.returncode != 0 or branch not in merged_names:
        return CleanupResult(False, False, "分支尚未合并到主工作区，已拒绝清理。")
    removed = _git(main, "worktree", "remove", str(path))
    if removed.returncode != 0:
        detail = removed.stderr.strip() or "git worktree remove 失败"
        return CleanupResult(False, False, detail)
    deleted = _git(main, "branch", "-d", branch)
    if deleted.returncode != 0:
        return CleanupResult(
            True,
            True,
            "工作区已删除，但分支未能自动删除，请手动检查。",
        )
    return CleanupResult(True, True, "已删除已合并的干净工作区和分支。")


__all__ = ["CleanupResult", "cleanup_merged_worktree"]
