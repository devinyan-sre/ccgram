from pathlib import Path
from unittest.mock import patch

from ccgram.handlers.text.member_lanes import LaneResult, _isolated_cwd
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
    eligibility = WorktreeEligibility(False, None, None, False, "not git")
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
