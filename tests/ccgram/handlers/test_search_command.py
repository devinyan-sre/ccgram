"""Tests for /search — cross-session transcript keyword search."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.handlers.search_command import (
    _MAX_HITS,
    SearchHit,
    format_results,
    search_command,
    search_transcripts,
)


def _write_entry(
    path: Path,
    *,
    entry_type: str = "user",
    text: str = "hello world",
    session_id: str = "sess-1",
    timestamp: str = "2026-07-23T10:00:00.000Z",
    **extra,
) -> None:
    entry = {
        "type": entry_type,
        "message": {"role": entry_type, "content": text},
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": "/home/user/proj",
        **extra,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _project(tmp_path: Path, name: str = "-home-user-proj") -> Path:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return d


class TestSearchTranscripts:
    def test_finds_match_in_user_message(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        _write_entry(f, text="please fix the flaky login test")

        hits, truncated = search_transcripts(tmp_path, "flaky login")
        assert len(hits) == 1
        assert hits[0].role == "user"
        assert "flaky login" in hits[0].snippet
        assert hits[0].project == "/home/user/proj"
        assert hits[0].timestamp == "2026-07-23 10:00"
        assert truncated is False

    def test_case_insensitive(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        _write_entry(f, text="Deploy the STAGING environment")

        hits, _ = search_transcripts(tmp_path, "staging")
        assert len(hits) == 1

    def test_assistant_content_blocks(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secretword here"},
                    {"type": "text", "text": "the answer is fourty-two"},
                ],
            },
            "sessionId": "s",
            "timestamp": "2026-07-23T10:00:00.000Z",
        }
        f.write_text(json.dumps(entry) + "\n")

        hits, _ = search_transcripts(tmp_path, "fourty-two")
        assert len(hits) == 1
        assert hits[0].role == "assistant"
        # Raw-line prefilter matches thinking blocks, but extracted text
        # must not — no hit for non-text content.
        hits, _ = search_transcripts(tmp_path, "secretword")
        assert hits == []

    def test_skips_meta_and_sidechain(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        _write_entry(f, text="needle", isMeta=True)
        _write_entry(f, text="needle", isSidechain=True)

        hits, _ = search_transcripts(tmp_path, "needle")
        assert hits == []

    def test_skips_non_message_types_and_bad_lines(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        f.write_text('{"type":"summary","summary":"needle"}\nnot-json needle\n')
        _write_entry(f, text="the needle is here")

        hits, _ = search_transcripts(tmp_path, "needle")
        assert len(hits) == 1

    def test_hit_cap_marks_truncated(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        for i in range(_MAX_HITS + 5):
            _write_entry(f, text=f"needle number {i}")

        hits, truncated = search_transcripts(tmp_path, "needle")
        assert len(hits) == _MAX_HITS
        assert truncated is True

    def test_no_matches(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        _write_entry(f, text="nothing to see")

        hits, truncated = search_transcripts(tmp_path, "unicorn")
        assert hits == []
        assert truncated is False

    def test_missing_projects_dir(self, tmp_path: Path) -> None:
        hits, truncated = search_transcripts(tmp_path / "missing", "x")
        assert hits == []
        assert truncated is False

    def test_snippet_truncates_long_text(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        _write_entry(f, text="a" * 500 + " needle " + "b" * 500)

        hits, _ = search_transcripts(tmp_path, "needle")
        assert len(hits) == 1
        assert hits[0].snippet.startswith("…")
        assert hits[0].snippet.endswith("…")
        assert len(hits[0].snippet) < 200


class TestFormatResults:
    def test_no_hits(self) -> None:
        text = format_results("foo", [], False)
        assert "No matches" in text
        assert "foo" in text

    def test_hits_rendered_with_meta(self) -> None:
        hit = SearchHit(
            project="/home/user/proj",
            session_id="abcdef1234567890",
            role="user",
            timestamp="2026-07-23 10:00",
            snippet="fix the login test",
        )
        text = format_results("login", [hit], False)
        assert "/home/user/proj" in text
        assert "abcdef12" in text
        assert "fix the login test" in text
        assert "truncated" not in text

    def test_truncated_notice(self) -> None:
        hit = SearchHit(
            project="p", session_id="s", role="user", timestamp="", snippet="x"
        )
        text = format_results("q", [hit], True)
        assert "truncated" in text


class TestSearchCommand:
    def _update(self, user_id: int = 12345, text: str = "/search foo"):
        update = MagicMock()
        update.effective_user.id = user_id
        update.message.text = text
        return update

    async def test_usage_without_args(self) -> None:
        update = self._update()
        context = MagicMock()
        context.args = []
        with patch(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_reply",
            new_callable=AsyncMock,
        ) as mock_reply:
            await search_command(update, context)
        mock_reply.assert_awaited_once()
        assert "Usage" in mock_reply.call_args.args[1]

    async def test_unauthorized_user_ignored(self) -> None:
        update = self._update(user_id=999)
        context = MagicMock()
        context.args = ["foo"]
        with patch(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_reply",
            new_callable=AsyncMock,
        ) as mock_reply:
            await search_command(update, context)
        mock_reply.assert_not_awaited()

    async def test_replies_with_results(self, tmp_path: Path) -> None:
        f = _project(tmp_path) / "a.jsonl"
        _write_entry(f, text="the needle is here")
        update = self._update()
        context = MagicMock()
        context.args = ["needle"]
        with (
            patch(
                "ccgram.config.config.claude_projects_path",
                tmp_path,
            ),
            patch(
                "ccgram.handlers.messaging_pipeline.message_sender.safe_reply",
                new_callable=AsyncMock,
            ) as mock_reply,
        ):
            await search_command(update, context)
        mock_reply.assert_awaited_once()
        assert "needle" in mock_reply.call_args.args[1]
