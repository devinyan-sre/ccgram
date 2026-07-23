"""Tests for event_reader — incremental events.jsonl reading + compaction."""

import json
from pathlib import Path

import pytest

from ccgram.event_reader import (
    _ARCHIVE_MAX_BYTES,
    compact_events_file,
    read_new_events,
)
from ccgram.providers.base import HookEvent


def _write_event(path: Path, event_type: str, window_key: str, session_id: str) -> None:
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "event": event_type,
                    "window_key": window_key,
                    "session_id": session_id,
                    "data": {},
                    "ts": 1234567890.0,
                }
            )
            + "\n"
        )


async def test_returns_empty_when_file_missing(tmp_path: Path) -> None:
    events, offset = await read_new_events(tmp_path / "missing.jsonl", 0)
    assert events == []
    assert offset == 0


async def test_reads_new_events_from_zero(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    _write_event(path, "SessionStart", "ccgram:@1", "sess-2")

    events, offset = await read_new_events(path, 0)
    assert len(events) == 2
    assert events[0].event_type == "Stop"
    assert events[0].window_key == "ccgram:@0"
    assert events[1].event_type == "SessionStart"
    assert offset == path.stat().st_size


async def test_reads_only_new_events_after_offset(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    _, offset_after_first = await read_new_events(path, 0)

    _write_event(path, "SessionStart", "ccgram:@1", "sess-2")
    events, offset = await read_new_events(path, offset_after_first)
    assert len(events) == 1
    assert events[0].event_type == "SessionStart"
    assert offset > offset_after_first


async def test_skips_empty_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("\n\n")
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    path.open("a").write("\n")

    events, offset = await read_new_events(path, 0)
    assert len(events) == 1
    assert events[0].event_type == "Stop"


async def test_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("not-json\n")
    _write_event(path, "Stop", "ccgram:@0", "sess-1")

    events, offset = await read_new_events(path, 0)
    assert len(events) == 1
    assert events[0].event_type == "Stop"


async def test_resets_offset_on_truncation(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    file_size = path.stat().st_size

    stale_offset = file_size + 9999
    events, offset = await read_new_events(path, stale_offset)
    assert offset <= file_size


async def test_skips_non_dict_json_line_and_advances_offset(tmp_path: Path) -> None:
    """A valid JSON line that is not a dict (e.g. []) must be skipped,
    the byte offset must advance past it, and subsequent valid events
    must still be read (regression for MAJOR-3 poll-wedge bug).
    """
    path = tmp_path / "events.jsonl"
    path.write_text("[]\n")  # valid JSON, not a dict
    offset_after_bad = path.stat().st_size
    _write_event(path, "Stop", "ccgram:@0", "sess-1")

    events, offset = await read_new_events(path, 0)
    assert len(events) == 1
    assert events[0].event_type == "Stop"
    # Offset must advance past the bad line — not stall at 0
    assert offset > offset_after_bad


async def test_returns_hook_event_dataclass(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Notification", "ccgram:@5", "abc-123")

    events, _ = await read_new_events(path, 0)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, HookEvent)
    assert ev.event_type == "Notification"
    assert ev.window_key == "ccgram:@5"
    assert ev.session_id == "abc-123"
    assert ev.timestamp == pytest.approx(1234567890.0)


# --- compact_events_file ---


async def test_compact_drops_consumed_prefix_keeps_tail(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    _, consumed_offset = await read_new_events(path, 0)
    _write_event(path, "Notification", "ccgram:@1", "sess-2")

    new_offset = compact_events_file(path, consumed_offset)
    assert new_offset == 0

    # Only the unconsumed event remains, readable from offset 0.
    events, _ = await read_new_events(path, 0)
    assert [e.event_type for e in events] == ["Notification"]


async def test_compact_fully_consumed_leaves_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    _, consumed_offset = await read_new_events(path, 0)

    new_offset = compact_events_file(path, consumed_offset)
    assert new_offset == 0
    assert path.stat().st_size == 0


def test_compact_noop_when_nothing_consumed(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    size = path.stat().st_size

    assert compact_events_file(path, 0) == 0
    assert path.stat().st_size == size


def test_compact_noop_when_file_missing(tmp_path: Path) -> None:
    assert compact_events_file(tmp_path / "missing.jsonl", 100) == 100


def test_compact_noop_when_offset_beyond_file(tmp_path: Path) -> None:
    """External truncation/recreation: leave the file for the reader's reset."""
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    size = path.stat().st_size

    new_offset = compact_events_file(path, size + 9999)
    assert new_offset == size + 9999
    assert path.stat().st_size == size


def test_compact_archives_consumed_bytes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    consumed = path.stat().st_size
    original = path.read_bytes()

    compact_events_file(path, consumed)
    archive = tmp_path / "events.jsonl.old"
    assert archive.read_bytes() == original

    # A second compaction appends to the archive.
    _write_event(path, "Notification", "ccgram:@1", "sess-2")
    second = path.read_bytes()
    compact_events_file(path, len(second))
    assert archive.read_bytes() == original + second


def test_compact_archive_overwrites_past_cap(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    archive = tmp_path / "events.jsonl.old"
    archive.write_bytes(b"x" * _ARCHIVE_MAX_BYTES)

    _write_event(path, "Stop", "ccgram:@0", "sess-1")
    consumed = path.stat().st_size
    original = path.read_bytes()

    compact_events_file(path, consumed)
    assert archive.read_bytes() == original  # overwritten, not appended
