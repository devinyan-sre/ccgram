"""Tests for /usage — transcript token accounting."""

import json

from ccgram.handlers.usage_command import (
    UsageTotals,
    collect_usage,
    format_usage,
)


def _write_transcript(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _assistant(usage=None, model="claude-opus-4-8"):
    msg = {"model": model}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "message": msg}


class TestCollectUsage:
    def test_sums_usage_across_turns(self, tmp_path) -> None:
        f = tmp_path / "t.jsonl"
        _write_transcript(
            f,
            [
                {"type": "user"},
                _assistant(
                    {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 200,
                    }
                ),
                {"type": "user"},
                _assistant({"input_tokens": 10, "output_tokens": 5}),
            ],
        )
        totals = collect_usage(f)
        assert totals is not None
        assert totals.input_tokens == 110
        assert totals.output_tokens == 55
        assert totals.cache_read_tokens == 1000
        assert totals.cache_creation_tokens == 200
        assert totals.user_turns == 2
        assert totals.assistant_turns == 2
        assert totals.models == {"claude-opus-4-8"}
        assert totals.has_data

    def test_skips_malformed_lines(self, tmp_path) -> None:
        f = tmp_path / "t.jsonl"
        f.write_text('not json\n{"type":"summary"}\n')
        totals = collect_usage(f)
        assert totals is not None
        assert not totals.has_data

    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert collect_usage(tmp_path / "missing.jsonl") is None

    def test_no_usage_data_has_data_false(self, tmp_path) -> None:
        f = tmp_path / "t.jsonl"
        _write_transcript(f, [{"type": "user"}, _assistant()])
        totals = collect_usage(f)
        assert totals is not None
        assert not totals.has_data


class TestFormatUsage:
    def test_formats_counts_with_units(self) -> None:
        totals = UsageTotals(
            input_tokens=1_500,
            output_tokens=2_000_000,
            cache_read_tokens=10,
            cache_creation_tokens=0,
            assistant_turns=3,
            user_turns=2,
            models={"claude-opus-4-8"},
        )
        text = format_usage(totals, "abcdef12-3456")
        assert "1.5K" in text
        assert "2.0M" in text
        assert "abcdef12" in text
        assert "claude-opus-4-8" in text
