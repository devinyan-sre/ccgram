"""Tests for token_watch — live context/cumulative token warnings."""

import pytest

from ccgram.config import config
from ccgram.token_watch import TokenWatch


def _assistant_entry(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    sidechain: bool = False,
) -> dict:
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    }
    if sidechain:
        entry["isSidechain"] = True
    return entry


@pytest.fixture
def watch(monkeypatch) -> TokenWatch:
    monkeypatch.setattr(config, "context_warn_pct", 80)
    monkeypatch.setattr(config, "context_limit_tokens", 200_000)
    monkeypatch.setattr(config, "token_warn_total", 0)
    return TokenWatch()


class TestContextWarning:
    def test_fires_at_threshold(self, watch: TokenWatch) -> None:
        # 80% of 200k = 160k
        warnings = watch.record_entries(
            "s1", [_assistant_entry(input_tokens=1000, cache_read=159_000)]
        )
        assert len(warnings) == 1
        assert "/compact" in warnings[0]
        assert "80%" in warnings[0]

    def test_below_threshold_silent(self, watch: TokenWatch) -> None:
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=100_000)])
        assert warnings == []

    def test_warns_once_while_high(self, watch: TokenWatch) -> None:
        watch.record_entries("s1", [_assistant_entry(cache_read=170_000)])
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=180_000)])
        assert warnings == []

    def test_rearms_after_compaction(self, watch: TokenWatch) -> None:
        watch.record_entries("s1", [_assistant_entry(cache_read=170_000)])
        # Compaction: context drops well below 70% of the 160k threshold.
        assert watch.record_entries("s1", [_assistant_entry(cache_read=30_000)]) == []
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=165_000)])
        assert len(warnings) == 1

    def test_sidechain_does_not_touch_context(self, watch: TokenWatch) -> None:
        watch.record_entries("s1", [_assistant_entry(cache_read=170_000)])
        # A small subagent turn must not re-arm the main context warning.
        watch.record_entries("s1", [_assistant_entry(cache_read=5_000, sidechain=True)])
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=171_000)])
        assert warnings == []

    def test_disabled_when_pct_zero(self, watch: TokenWatch, monkeypatch) -> None:
        monkeypatch.setattr(config, "context_warn_pct", 0)
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=199_000)])
        assert warnings == []


class TestTotalWarning:
    def test_fires_once_past_threshold(self, watch: TokenWatch, monkeypatch) -> None:
        monkeypatch.setattr(config, "token_warn_total", 100_000)
        assert watch.record_entries("s1", [_assistant_entry(cache_read=60_000)]) == []
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=60_000)])
        assert len(warnings) == 1
        assert "120k" in warnings[0]
        # Never again for this session.
        assert watch.record_entries("s1", [_assistant_entry(cache_read=60_000)]) == []

    def test_sidechain_counts_toward_total(
        self, watch: TokenWatch, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "token_warn_total", 50_000)
        warnings = watch.record_entries(
            "s1", [_assistant_entry(cache_read=60_000, sidechain=True)]
        )
        assert len(warnings) == 1

    def test_disabled_by_default(self, watch: TokenWatch, monkeypatch) -> None:
        assert config.token_warn_total == 0  # default: cumulative warn off
        monkeypatch.setattr(config, "context_warn_pct", 0)
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=10_000_000)])
        assert warnings == []


class TestHygiene:
    def test_non_assistant_and_missing_usage_ignored(self, watch: TokenWatch) -> None:
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {}},
            {"type": "summary"},
        ]
        assert watch.record_entries("s1", entries) == []

    def test_sessions_isolated(self, watch: TokenWatch) -> None:
        watch.record_entries("s1", [_assistant_entry(cache_read=170_000)])
        warnings = watch.record_entries("s2", [_assistant_entry(cache_read=170_000)])
        assert len(warnings) == 1  # s2 warns independently

    def test_clear_session_resets(self, watch: TokenWatch) -> None:
        watch.record_entries("s1", [_assistant_entry(cache_read=170_000)])
        watch.clear_session("s1")
        warnings = watch.record_entries("s1", [_assistant_entry(cache_read=170_000)])
        assert len(warnings) == 1
