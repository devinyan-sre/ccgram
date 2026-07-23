"""Tests for fs_watcher — filesystem-event wakeups for the session monitor."""

import asyncio
from pathlib import Path

import pytest

from ccgram.fs_watcher import TranscriptWatcher
from ccgram.session_monitor import SessionMonitor

_WAIT = 3.0  # generous inotify delivery timeout for slow CI


@pytest.fixture
def wake_event() -> asyncio.Event:
    return asyncio.Event()


async def _make_watcher(
    paths: list[Path], wake_event: asyncio.Event
) -> TranscriptWatcher:
    return TranscriptWatcher(paths, wake_event, asyncio.get_running_loop())


async def test_jsonl_write_sets_wake_event(
    tmp_path: Path, wake_event: asyncio.Event
) -> None:
    watcher = await _make_watcher([tmp_path], wake_event)
    assert watcher.start() is True
    try:
        (tmp_path / "session.jsonl").write_text("{}\n")
        await asyncio.wait_for(wake_event.wait(), timeout=_WAIT)
    finally:
        watcher.stop()


async def test_jsonl_write_in_new_subdir_sets_wake_event(
    tmp_path: Path, wake_event: asyncio.Event
) -> None:
    """Recursive watch picks up transcripts in project subdirs created later."""
    watcher = await _make_watcher([tmp_path], wake_event)
    assert watcher.start() is True
    try:
        sub = tmp_path / "-home-user-proj"
        sub.mkdir()
        await asyncio.sleep(0.2)  # let the observer register the new dir
        (sub / "abc.jsonl").write_text("{}\n")
        await asyncio.wait_for(wake_event.wait(), timeout=_WAIT)
    finally:
        watcher.stop()


async def test_non_jsonl_write_does_not_wake(
    tmp_path: Path, wake_event: asyncio.Event
) -> None:
    watcher = await _make_watcher([tmp_path], wake_event)
    assert watcher.start() is True
    try:
        (tmp_path / "state.json").write_text("{}")
        (tmp_path / "notes.txt").write_text("hi")
        await asyncio.sleep(0.3)
        assert not wake_event.is_set()
    finally:
        watcher.stop()


async def test_start_returns_false_when_no_paths_exist(
    tmp_path: Path, wake_event: asyncio.Event
) -> None:
    watcher = await _make_watcher([tmp_path / "missing"], wake_event)
    assert watcher.start() is False


async def test_stop_is_idempotent(tmp_path: Path, wake_event: asyncio.Event) -> None:
    watcher = await _make_watcher([tmp_path], wake_event)
    assert watcher.start() is True
    watcher.stop()
    watcher.stop()  # second call must not raise


class TestWaitForNextCycle:
    async def test_plain_sleep_without_watcher(self, tmp_path: Path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path,
            poll_interval=0.01,
            state_file=tmp_path / "monitor_state.json",
        )
        await asyncio.wait_for(monitor._wait_for_next_cycle(), timeout=1.0)

    async def test_wakes_early_on_event(self, tmp_path: Path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path,
            poll_interval=30.0,  # would time the test out without the wakeup
            state_file=tmp_path / "monitor_state.json",
        )
        monitor._fs_watcher = object()  # type: ignore[assignment]  # any non-None sentinel
        monitor._wake_event.set()

        await asyncio.wait_for(monitor._wait_for_next_cycle(), timeout=1.0)
        assert not monitor._wake_event.is_set()  # cleared for the next cycle
