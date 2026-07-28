"""Tests for the daily digest job."""

import datetime as dt
import json
from unittest.mock import MagicMock, patch

from ccgram.handlers.daily_digest import (
    _idle_suffix,
    build_digest_for_user,
    scan_transcript,
    setup_daily_digest_job,
)
from ccgram.handlers.digest_text import DigestStats


def _entry(entry_type: str, ts: dt.datetime, content=None) -> dict:
    return {
        "type": entry_type,
        "timestamp": ts.isoformat() + "Z",
        "message": {"content": "内容" if content is None else content},
    }


class TestScanTranscript:
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
        stats, _ = scan_transcript(f, since)
        assert stats.prompts == 1
        assert stats.replies == 2

    def test_tool_traffic_is_not_conversation(self, tmp_path) -> None:
        """The bug this replaced: tool results counted as prompts."""
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        f = tmp_path / "t.jsonl"
        f.write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    _entry("user", now, "真实提问"),
                    _entry("user", now, [{"type": "tool_result", "content": "ok"}]),
                    _entry("user", now, [{"type": "tool_result", "content": "ok"}]),
                    _entry(
                        "assistant",
                        now,
                        [{"type": "tool_use", "name": "Bash", "input": {}}],
                    ),
                ]
            )
        )
        stats, _ = scan_transcript(f, 0)
        assert stats.prompts == 1
        assert stats.replies == 0
        assert stats.tools == (("Bash", 1),)

    def test_last_ts_tracks_the_newest_entry(self, tmp_path) -> None:
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        f = tmp_path / "t.jsonl"
        f.write_text(json.dumps(_entry("user", now)) + "\n")
        _, last_ts = scan_transcript(f, 0)
        assert last_ts is not None
        assert abs(last_ts - now.timestamp()) < 2

    def test_missing_file_returns_empty(self, tmp_path) -> None:
        stats, last_ts = scan_transcript(tmp_path / "nope.jsonl", 0)
        assert stats == DigestStats()
        assert last_ts is None

    def test_malformed_lines_skipped(self, tmp_path) -> None:
        f = tmp_path / "t.jsonl"
        f.write_text("not json\n{}\n[]\n")
        stats, _ = scan_transcript(f, 0)
        assert stats == DigestStats()


class TestIdleSuffix:
    def test_recent_is_active(self) -> None:
        now = 1_000_000.0
        assert "🟢" in _idle_suffix(now - 60, now)

    def test_hours_when_under_a_day(self) -> None:
        now = 1_000_000.0
        assert "5" in _idle_suffix(now - 5 * 3600, now)

    def test_days_when_over_a_day(self) -> None:
        now = 1_000_000.0
        assert "2" in _idle_suffix(now - 2 * 86400, now)

    def test_no_timestamp_yields_nothing(self) -> None:
        assert _idle_suffix(None, 1_000_000.0) == ""


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
