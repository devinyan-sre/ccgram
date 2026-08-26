import subprocess

from ccgram.worktree_cleanup import cleanup_merged_worktree


def _git(path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _repo_with_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    lane = tmp_path / "lane"
    _git(repo, "worktree", "add", "-b", "ccg/member", str(lane), "HEAD")
    return repo, lane


def test_clean_merged_worktree_is_removed(tmp_path) -> None:
    repo, lane = _repo_with_worktree(tmp_path)
    (lane / "change.txt").write_text("change", encoding="utf-8")
    _git(lane, "add", ".")
    _git(lane, "commit", "-m", "change")
    _git(repo, "merge", "--ff-only", "ccg/member")

    result = cleanup_merged_worktree(lane, "ccg/member")

    assert result.removed is True
    assert not lane.exists()


def test_dirty_worktree_is_never_removed(tmp_path) -> None:
    _repo, lane = _repo_with_worktree(tmp_path)
    (lane / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = cleanup_merged_worktree(lane, "ccg/member")

    assert result.removed is False
    assert "未提交" in result.reason
    assert lane.exists()
