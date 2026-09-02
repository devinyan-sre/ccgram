from pathlib import Path
from unittest.mock import patch

from ccgram.handlers.text.member_lanes import (
    LaneResult,
    _isolated_cwd,
    _parallel_task_cwd,
)
from ccgram.handlers.topics.worktree import WorktreeEligibility

_MOD = "ccgram.handlers.text.member_lanes"


async def test_non_git_workspace_fails_closed() -> None:
    eligibility = WorktreeEligibility(False, None, None, False, "not git")
    with (
        patch(f"{_MOD}.config.member_lane_worktrees", True),
        patch(f"{_MOD}.config.allow_shared_member_cwd", False),
        patch(f"{_MOD}.check_worktree_eligibility", return_value=eligibility),
    ):
        result = await _isolated_cwd("/srv/runbooks", user_id=10, thread_id=7)

    assert isinstance(result, LaneResult)
    assert result.ready is False
    assert "blocked" in result.error


async def test_shared_non_git_workspace_requires_explicit_opt_in() -> None:
    eligibility = WorktreeEligibility(False, None, None, False, "not a git work tree")
    with (
        patch(f"{_MOD}.config.member_lane_worktrees", True),
        patch(f"{_MOD}.config.allow_shared_member_cwd", True),
        patch(f"{_MOD}.check_worktree_eligibility", return_value=eligibility),
    ):
        result = await _isolated_cwd("/srv/runbooks", user_id=10, thread_id=7)

    assert result == ("/srv/runbooks", "", "")


async def test_dirty_git_workspace_is_never_cloned() -> None:
    eligibility = WorktreeEligibility(
        True,
        Path("/repo"),
        "main",
        True,
        dirty_paths=("runbook.md",),
    )
    with (
        patch(f"{_MOD}.config.member_lane_worktrees", True),
        patch(f"{_MOD}.config.allow_shared_member_cwd", True),
        patch(f"{_MOD}.check_worktree_eligibility", return_value=eligibility),
    ):
        result = await _isolated_cwd("/repo", user_id=10, thread_id=7)

    assert isinstance(result, LaneResult)
    assert "uncommitted" in result.error
    assert "runbook.md" in result.error


async def test_parallel_task_non_git_workspace_explains_safe_opt_in() -> None:
    eligibility = WorktreeEligibility(False, None, None, False, "not a git work tree")
    with (
        patch(f"{_MOD}.config.parallel_task_worktrees", True),
        patch(f"{_MOD}.config.allow_shared_member_cwd", False),
        patch(f"{_MOD}.check_worktree_eligibility", return_value=eligibility),
    ):
        result = await _parallel_task_cwd(
            "/srv/runbooks", user_id=10, thread_id=7, task_id="T0042"
        )

    assert isinstance(result, LaneResult)
    assert result.ready is False
    assert "不是 Git 仓库" in result.error
    assert "CCGRAM_ALLOW_SHARED_MEMBER_CWD=true" in result.error


async def test_parallel_task_non_git_workspace_can_share_when_enabled() -> None:
    eligibility = WorktreeEligibility(False, None, None, False, "not a git work tree")
    with (
        patch(f"{_MOD}.config.parallel_task_worktrees", True),
        patch(f"{_MOD}.config.allow_shared_member_cwd", True),
        patch(f"{_MOD}.check_worktree_eligibility", return_value=eligibility),
        patch(f"{_MOD}.create_worktree") as create_worktree,
    ):
        result = await _parallel_task_cwd(
            "/srv/runbooks", user_id=10, thread_id=7, task_id="T0042"
        )

    assert result == ("/srv/runbooks", "", "")
    create_worktree.assert_not_called()


async def test_parallel_task_unsafe_git_state_never_falls_back_to_shared() -> None:
    eligibility = WorktreeEligibility(False, None, None, False, "detached HEAD")
    with (
        patch(f"{_MOD}.config.parallel_task_worktrees", True),
        patch(f"{_MOD}.config.allow_shared_member_cwd", True),
        patch(f"{_MOD}.check_worktree_eligibility", return_value=eligibility),
    ):
        result = await _parallel_task_cwd(
            "/srv/repo", user_id=10, thread_id=7, task_id="T0042"
        )

    assert isinstance(result, LaneResult)
    assert result.ready is False
    assert "detached HEAD" in result.error
