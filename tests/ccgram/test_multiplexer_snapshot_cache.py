"""Short-TTL topology-cache behaviour of both multiplexer backends.

The 1s status poll, the 2s session monitor, live views, and per-tick
provider detection all list windows / resolve foreground processes. These
tests pin the caching contract added to kill the per-tick fork storm:

- ``TmuxManager.list_windows`` serves a snapshot within ``WINDOW_CACHE_TTL``
  and re-lists after any window mutation.
- ``HerdrManager`` shares one ``pane list`` / ``workspace list`` answer per
  TTL window and invalidates on tab mutations.
- ``process_detection.foreground_cached`` reuses a ``foreground()`` answer
  for ``FOREGROUND_TTL`` seconds and resets via ``clear_detection_cache``.
"""

import json
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.multiplexer.base import ForegroundInfo
from ccgram.multiplexer.herdr import HerdrManager
from ccgram.multiplexer.tmux import TmuxManager
from ccgram.providers.process_detection import (
    clear_detection_cache,
    foreground_cached,
)

# ── tmux: list_windows snapshot ────────────────────────────────────────


def _tmux_manager(ttl: float | None = None) -> tuple[TmuxManager, MagicMock]:
    mgr = TmuxManager(session_name="ccgram-test", window_cache_ttl=ttl)
    session_mock = MagicMock(return_value=None)
    mgr.get_session = session_mock  # type: ignore[method-assign]
    return mgr, session_mock


async def test_tmux_list_windows_served_from_cache_within_ttl() -> None:
    mgr, session_mock = _tmux_manager()
    assert await mgr.list_windows() == []
    assert await mgr.list_windows() == []
    # Only the first call touched tmux; the second hit the snapshot.
    assert session_mock.call_count == 1


async def test_tmux_zero_ttl_disables_cache() -> None:
    mgr, session_mock = _tmux_manager(ttl=0)
    await mgr.list_windows()
    await mgr.list_windows()
    assert session_mock.call_count == 2


async def test_tmux_mutations_invalidate_cache() -> None:
    mgr, session_mock = _tmux_manager()
    await mgr.list_windows()
    await mgr.kill_window("@1")  # session None → returns False, still invalidates
    await mgr.list_windows()
    # get_session: list(1) + kill(1) + list(1)
    assert session_mock.call_count == 3


async def test_tmux_reset_server_drops_cache() -> None:
    mgr, session_mock = _tmux_manager()
    await mgr.list_windows()
    mgr._reset_server()
    await mgr.list_windows()
    assert session_mock.call_count == 2


# ── herdr: pane list / workspace labels / window snapshot ──────────────


class _FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses

    async def __call__(self, args: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        for key, out in self._responses.items():
            if list(key) == list(args)[: len(key)]:
                return (0, out, "")
        return (1, "", "no canned response")

    def count(self, *prefix: str) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == list(prefix))


def _ok(payload: dict) -> str:
    return json.dumps({"id": "x", "result": payload})


def _herdr(runner: _FakeRunner) -> HerdrManager:
    return HerdrManager(socket_path="/tmp/herdr.sock", runner=runner)


def _herdr_runner() -> _FakeRunner:
    return _FakeRunner(
        {
            ("tab", "list"): _ok(
                {
                    "tabs": [
                        {"label": "proj", "tab_id": "w1:t1", "workspace_id": "w1"}
                    ]
                }
            ),
            ("pane", "list"): _ok(
                {
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "tab_id": "w1:t1",
                            "agent": "claude",
                            "focused": True,
                            "cwd": "/proj",
                        }
                    ]
                }
            ),
            ("workspace", "list"): _ok(
                {"workspaces": [{"workspace_id": "w1", "label": "proj-ws"}]}
            ),
            ("tab", "close"): _ok({"ok": True}),
        }
    )


async def test_herdr_list_windows_snapshot_within_ttl() -> None:
    runner = _herdr_runner()
    mgr = _herdr(runner)
    first = await mgr.list_windows()
    second = await mgr.list_windows()
    assert first == second
    assert runner.count("tab", "list") == 1
    assert runner.count("pane", "list") == 1
    assert runner.count("workspace", "list") == 1


async def test_herdr_pane_list_shared_across_tab_resolutions() -> None:
    runner = _herdr_runner()
    mgr = _herdr(runner)
    assert await mgr._active_pane("w1:t1") == "w1:p1"
    assert await mgr._active_pane("w1:t1") == "w1:p1"
    assert runner.count("pane", "list") == 1


async def test_herdr_kill_window_invalidates_topology() -> None:
    runner = _herdr_runner()
    mgr = _herdr(runner)
    await mgr.list_windows()
    assert await mgr.kill_window("w1:t1") is True
    await mgr.list_windows()
    assert runner.count("tab", "list") == 2
    assert runner.count("pane", "list") == 2


async def test_herdr_reconciliation_stays_uncached() -> None:
    runner = _herdr_runner()
    mgr = _herdr(runner)
    await mgr.list_windows_for_reconciliation()
    await mgr.list_windows_for_reconciliation()
    # tab list is the freshness anchor — never served from the snapshot.
    assert runner.count("tab", "list") == 2


# ── foreground_cached ──────────────────────────────────────────────────


async def test_foreground_cached_reuses_answer_within_ttl() -> None:
    clear_detection_cache()
    fg = ForegroundInfo(pid=1, pgid=1, argv=["claude"], cwd="/p")
    mock_mux = MagicMock()
    mock_mux.foreground = AsyncMock(return_value=fg)
    try:
        with patch("ccgram.multiplexer.multiplexer", mock_mux):
            assert await foreground_cached("@9") is fg
            assert await foreground_cached("@9") is fg
        assert mock_mux.foreground.await_count == 1
    finally:
        clear_detection_cache()


async def test_foreground_cached_cleared_by_clear_detection_cache() -> None:
    clear_detection_cache()
    fg = ForegroundInfo(pid=1, pgid=1, argv=["claude"], cwd="/p")
    mock_mux = MagicMock()
    mock_mux.foreground = AsyncMock(return_value=fg)
    try:
        with patch("ccgram.multiplexer.multiplexer", mock_mux):
            await foreground_cached("@9")
            clear_detection_cache("@9")
            await foreground_cached("@9")
        assert mock_mux.foreground.await_count == 2
    finally:
        clear_detection_cache()
