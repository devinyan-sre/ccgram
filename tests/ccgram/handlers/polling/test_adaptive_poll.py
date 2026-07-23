"""Adaptive poll backoff — pure decision kernel + coordinator skip wiring.

Idle windows (no pane-content change, no transcript activity for
``IDLE_BACKOFF_AFTER``) tick only every ``IDLE_TICK_EVERY`` cycles; any
activity signal restores per-cycle cadence on the next loop iteration.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.handlers.polling.window_tick import tick_window
from ccgram.handlers.polling.polling_types import (
    IDLE_BACKOFF_AFTER,
    IDLE_TICK_EVERY,
    WindowPollState,
    should_skip_idle_tick,
)
from ccgram.providers.base import StatusUpdate

NOW = 1_000_000.0


def _idle_ws(**overrides: object) -> WindowPollState:
    """WindowPollState whose pane content went stale past the threshold."""
    ws = WindowPollState()
    ws.last_change_ts = NOW - IDLE_BACKOFF_AFTER - 1.0
    for key, value in overrides.items():
        setattr(ws, key, value)
    return ws


class TestShouldSkipIdleTick:
    def test_no_state_never_skips(self) -> None:
        assert should_skip_idle_tick(None, None, NOW) is False

    def test_recent_pane_change_never_skips(self) -> None:
        ws = WindowPollState()
        ws.last_change_ts = NOW - 1.0
        assert should_skip_idle_tick(ws, None, NOW) is False

    def test_idle_window_skips(self) -> None:
        assert should_skip_idle_tick(_idle_ws(), None, NOW) is True

    def test_recent_transcript_activity_never_skips(self) -> None:
        assert should_skip_idle_tick(_idle_ws(), NOW - 2.0, NOW) is False

    def test_stale_transcript_activity_still_skips(self) -> None:
        stale = NOW - IDLE_BACKOFF_AFTER - 5.0
        assert should_skip_idle_tick(_idle_ws(), stale, NOW) is True

    def test_skip_budget_exhausted_ticks(self) -> None:
        ws = _idle_ws(skipped_ticks=IDLE_TICK_EVERY - 1)
        assert should_skip_idle_tick(ws, None, NOW) is False

    def test_queue_pending_never_skips(self) -> None:
        assert should_skip_idle_tick(_idle_ws(), None, NOW, queue_empty=False) is False

    def test_interactive_ui_never_skips(self) -> None:
        interactive = StatusUpdate(
            raw_text="pick one", display_label="AskUser", is_interactive=True
        )
        ws = _idle_ws(last_pyte_result=interactive)
        assert should_skip_idle_tick(ws, None, NOW) is False

    def test_non_interactive_result_still_skips(self) -> None:
        status = StatusUpdate(raw_text="✻ done", display_label="done")
        ws = _idle_ws(last_pyte_result=status)
        assert should_skip_idle_tick(ws, None, NOW) is True

    def test_rc_active_never_skips(self) -> None:
        assert should_skip_idle_tick(_idle_ws(rc_active=True), None, NOW) is False


def _runtime_with(ws: WindowPollState | None) -> MagicMock:
    rt = MagicMock()
    rt.lifecycle.is_dead_notified.return_value = False
    rt.poll_state.peek_state.return_value = ws
    return rt


_WT = "ccgram.handlers.polling.window_tick"
_ACTIVITY = f"{_WT}._get_last_activity_ts"
_QUEUE = f"{_WT}.get_message_queue"
_DISCOVER = f"{_WT}.discover_and_register_transcript"
_UPDATE = f"{_WT}._update_status"
_SCAN = f"{_WT}._scan_window_panes"
_PASSIVE = f"{_WT}._maybe_check_passive_shell"
_DEAD = f"{_WT}._handle_dead_window_notification"
_INTERACTIVE = f"{_WT}._check_interactive_only"


def _patch_tick_deps(**overrides: object):
    """Patch tick_window's collaborators; yields the mock dict."""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        with (
            patch(_QUEUE, return_value=overrides.get("queue")) as q,
            patch(_ACTIVITY, return_value=overrides.get("activity")) as a,
            patch(_DISCOVER, new_callable=AsyncMock) as d,
            patch(_UPDATE, new_callable=AsyncMock) as u,
            patch(_SCAN, new_callable=AsyncMock) as s,
            patch(_PASSIVE, new_callable=AsyncMock) as p,
            patch(_DEAD, new_callable=AsyncMock) as dead,
            patch(_INTERACTIVE, new_callable=AsyncMock),
        ):
            yield {
                "queue": q,
                "activity": a,
                "discover": d,
                "update": u,
                "scan": s,
                "passive": p,
                "dead": dead,
            }

    return ctx()


class TestTickWindowAdaptive:
    async def test_idle_window_ticks_every_nth_cycle(self) -> None:
        ws = WindowPollState()
        ws.last_change_ts = time.time() - IDLE_BACKOFF_AFTER - 10.0
        rt = _runtime_with(ws)
        with _patch_tick_deps() as mocks:
            for _ in range(IDLE_TICK_EVERY - 1):
                await tick_window(MagicMock(), 1, 100, "@0", MagicMock(), runtime=rt)
            mocks["update"].assert_not_called()
            mocks["discover"].assert_not_called()
            assert ws.skipped_ticks == IDLE_TICK_EVERY - 1

            await tick_window(MagicMock(), 1, 100, "@0", MagicMock(), runtime=rt)
            mocks["update"].assert_called_once()
            assert ws.skipped_ticks == 0

    async def test_transcript_activity_restores_cadence(self) -> None:
        ws = WindowPollState()
        ws.last_change_ts = time.time() - IDLE_BACKOFF_AFTER - 10.0
        rt = _runtime_with(ws)
        with _patch_tick_deps() as mocks:
            await tick_window(MagicMock(), 1, 100, "@0", MagicMock(), runtime=rt)
            mocks["update"].assert_not_called()  # idle → skipped

            # hook/inotify transcript activity arrives
            mocks["activity"].return_value = time.time()
            await tick_window(MagicMock(), 1, 100, "@0", MagicMock(), runtime=rt)
            mocks["update"].assert_called_once()
            assert ws.skipped_ticks == 0

    async def test_adaptive_disabled_always_ticks(self) -> None:
        ws = WindowPollState()
        ws.last_change_ts = time.time() - IDLE_BACKOFF_AFTER - 10.0
        rt = _runtime_with(ws)
        with _patch_tick_deps() as mocks:
            await tick_window(
                MagicMock(), 1, 100, "@0", MagicMock(), runtime=rt, adaptive=False
            )
            mocks["update"].assert_called_once()

    async def test_dead_window_never_skipped(self) -> None:
        ws = WindowPollState()
        ws.last_change_ts = time.time() - IDLE_BACKOFF_AFTER - 10.0
        rt = _runtime_with(ws)
        with _patch_tick_deps() as mocks:
            await tick_window(MagicMock(), 1, 100, "@0", None, runtime=rt)
            mocks["dead"].assert_called_once()

    async def test_pending_queue_never_skipped(self) -> None:
        queue = MagicMock()
        queue.empty.return_value = False
        ws = WindowPollState()
        ws.last_change_ts = time.time() - IDLE_BACKOFF_AFTER - 10.0
        rt = _runtime_with(ws)
        with _patch_tick_deps(queue=queue) as mocks:
            await tick_window(MagicMock(), 1, 100, "@0", MagicMock(), runtime=rt)
            # queue-pending branch runs (interactive-only checks), not skipped
            mocks["scan"].assert_called_once()

    async def test_fresh_window_without_state_ticks(self) -> None:
        rt = _runtime_with(None)
        with _patch_tick_deps() as mocks:
            await tick_window(MagicMock(), 1, 100, "@0", MagicMock(), runtime=rt)
            mocks["update"].assert_called_once()
