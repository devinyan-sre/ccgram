"""Tests for /diff — git working-tree diff delivery to a topic."""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.handlers.diff_command import build_diff_report, send_diff


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")


class TestBuildDiffReport:
    def test_not_a_git_repository(self, tmp_path) -> None:
        result = build_diff_report(str(tmp_path), [])
        assert isinstance(result, str)
        assert "Not a git repository" in result

    def test_clean_tree(self, tmp_path) -> None:
        _init_repo(tmp_path)
        result = build_diff_report(str(tmp_path), [])
        assert isinstance(result, str)
        assert "clean" in result

    def test_modified_file_produces_summary_and_diff(self, tmp_path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("one\ntwo\n")
        result = build_diff_report(str(tmp_path), [])
        assert isinstance(result, tuple)
        summary, diff_text = result
        assert "a.txt" in summary
        assert "+two" in diff_text

    def test_untracked_only_shows_status(self, tmp_path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("x\n")
        result = build_diff_report(str(tmp_path), [])
        assert isinstance(result, tuple)
        summary, diff_text = result
        assert "new.txt" in summary
        assert diff_text.strip() == ""

    def test_path_filter_narrows_diff(self, tmp_path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("changed\n")
        (tmp_path / "b.txt").write_text("b\n")
        _git(tmp_path, "add", "b.txt")
        _git(tmp_path, "commit", "-qm", "b")
        (tmp_path / "b.txt").write_text("b2\n")
        result = build_diff_report(str(tmp_path), ["b.txt"])
        assert isinstance(result, tuple)
        _, diff_text = result
        assert "b2" in diff_text
        assert "changed" not in diff_text


class TestSendDiff:
    async def test_short_diff_sent_inline(self, tmp_path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("one\ntwo\n")
        client = MagicMock()
        with patch(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_send",
            new_callable=AsyncMock,
        ) as mock_send:
            await send_diff(client, 100, 42, "@1", str(tmp_path), [])
        mock_send.assert_awaited_once()
        text = mock_send.call_args.args[2]
        assert "```diff" in text
        client.send_document.assert_not_called()

    async def test_long_diff_sent_as_document(self, tmp_path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x\n" * 5000)
        client = MagicMock()
        client.send_document = AsyncMock()
        with patch(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_send",
            new_callable=AsyncMock,
        ) as mock_send:
            await send_diff(client, 100, 42, "@1", str(tmp_path), [])
        mock_send.assert_awaited_once()  # summary
        client.send_document.assert_awaited_once()
        assert client.send_document.call_args.kwargs["filename"] == "diff-1.diff"

    async def test_error_string_forwarded(self, tmp_path) -> None:
        client = MagicMock()
        with patch(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_send",
            new_callable=AsyncMock,
        ) as mock_send:
            await send_diff(client, 100, 42, "@1", str(tmp_path), [])
        text = mock_send.call_args.args[2]
        assert "Not a git repository" in text
