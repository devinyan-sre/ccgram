"""Tests for the daily digest job."""

import datetime as dt
import json
from unittest.mock import MagicMock, patch

from ccgram.handlers.daily_digest import (
    build_digest_for_user,
    count_recent_turns,
    setup_daily_digest_job,
)


def _entry(entry_type: str, ts: dt.datetime) -> dict:
    return {"type": entry_type, "timestamp": ts.isoformat() + "Z"}


class TestCountRecentTurns:
    def test_counts_only_recent(self, tmp_path) -> None:
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        old = now - dt.timedelta(days=2)
        f = tmp_path / "t.jsonl"
        f.write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    _entry("user", old),
                    _entry("assistant", old),
                    _entry("user", now),
                    _entry("assistant", now),
                    _entry("assistant", now),
                ]
            )
        )
        since = (now - dt.timedelta(days=1)).timestamp()
        users, assistants = count_recent_turns(f, since)
        assert users == 1
        assert assistants == 2

    def test_missing_file_returns_zero(self, tmp_path) -> None:
        assert count_recent_turns(tmp_path / "nope.jsonl", 0) == (0, 0)

    def test_malformed_lines_skipped(self, tmp_path) -> None:
        f = tmp_path / "t.jsonl"
        f.write_text("not json\n{}\n")
        assert count_recent_turns(f, 0) == (0, 0)


class TestBuildDigest:
    async def test_builds_lines_per_window(self, tmp_path) -> None:
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        f = tmp_path / "t.jsonl"
        f.write_text(json.dumps(_entry("user", now)) + "\n")

        view = MagicMock(provider_name="claude", transcript_path=f)
        with (
            patch("ccgram.handlers.daily_digest.view_window", return_value=view),
            patch("ccgram.handlers.daily_digest.thread_router") as mock_router,
        ):
            mock_router.get_display_name.return_value = "myproj"
            text = await build_digest_for_user(1, ["@1"])

        assert "myproj" in text
        assert "claude" in text
        assert "1" in text

    async def test_no_transcript_window(self) -> None:
        with (
            patch("ccgram.handlers.daily_digest.view_window", return_value=None),
            patch("ccgram.handlers.daily_digest.thread_router") as mock_router,
        ):
            mock_router.get_display_name.return_value = "ghost"
            text = await build_digest_for_user(1, ["@9"])
        assert "ghost" in text
        assert "no transcript" in text


class TestSetup:
    def test_disabled_when_unset(self) -> None:
        app = MagicMock()
        with patch("ccgram.config.config") as mock_config:
            mock_config.daily_digest_time = ""
            setup_daily_digest_job(app)
        app.job_queue.run_daily.assert_not_called()

    def test_scheduled_when_configured(self) -> None:
        app = MagicMock()
        with patch("ccgram.config.config") as mock_config:
            mock_config.daily_digest_time = "08:30"
            setup_daily_digest_job(app)
        app.job_queue.run_daily.assert_called_once()
        assert app.job_queue.run_daily.call_args.kwargs["time"] == dt.time(8, 30)

    def test_invalid_spec_skipped(self) -> None:
        app = MagicMock()
        with patch("ccgram.config.config") as mock_config:
            mock_config.daily_digest_time = "25:99"
            setup_daily_digest_job(app)
        app.job_queue.run_daily.assert_not_called()
