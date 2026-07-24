import time

import pytest

from ccgram.handlers.polling.polling_types import (
    STARTUP_TIMEOUT,
    TYPING_MAX_QUIET,
    TickContext,
)
from ccgram.handlers.polling.window_tick.decide import (
    build_status_line,
    decide_tick,
    is_shell_prompt,
)
from ccgram.providers.base import StatusUpdate


def _make_ctx(
    *,
    window_id: str = "@0",
    resolved_status_text: str | None = None,
    is_shell_prompt: bool = False,
    has_seen_status: bool = False,
    is_recently_active: bool = False,
    startup_time: float | None = None,
    is_dead_window: bool = False,
    supports_hook: bool = True,
    last_activity_ts: float | None = None,
    typing_enabled: bool = True,
) -> TickContext:
    return TickContext(
        window_id=window_id,
        resolved_status_text=resolved_status_text,
        is_shell_prompt=is_shell_prompt,
        has_seen_status=has_seen_status,
        is_recently_active=is_recently_active,
        startup_time=startup_time,
        is_dead_window=is_dead_window,
        supports_hook=supports_hook,
        last_activity_ts=last_activity_ts,
        typing_enabled=typing_enabled,
    )


class TestDecideTickDeadWindow:
    def test_dead_window_yields_recovery(self):
        ctx = _make_ctx(is_dead_window=True)
        decision = decide_tick(ctx)
        assert decision.show_recovery is True
        assert decision.transition is None

    def test_dead_window_overrides_other_signals(self):
        ctx = _make_ctx(
            is_dead_window=True,
            resolved_status_text="Working",
            is_recently_active=True,
            is_shell_prompt=True,
        )
        decision = decide_tick(ctx)
        assert decision.show_recovery is True


class TestDecideTickActiveStatus:
    def test_resolved_status_yields_active_with_text(self):
        ctx = _make_ctx(resolved_status_text="Working...")
        decision = decide_tick(ctx)
        assert decision.transition == "active"
        assert decision.send_status is True
        assert decision.status_text == "Working..."

    def test_recently_active_alone_yields_active_no_status(self):
        ctx = _make_ctx(is_recently_active=True)
        decision = decide_tick(ctx)
        assert decision.transition == "active"
        assert decision.send_status is False
        assert decision.status_text is None

    def test_resolved_status_takes_precedence_over_shell_prompt(self):
        ctx = _make_ctx(resolved_status_text="Working", is_shell_prompt=True)
        decision = decide_tick(ctx)
        assert decision.transition == "active"
        assert decision.send_status is True


class TestDecideTickShellPrompt:
    def test_hook_provider_yields_done(self):
        ctx = _make_ctx(is_shell_prompt=True, supports_hook=True)
        decision = decide_tick(ctx)
        assert decision.transition == "done"

    def test_no_hook_provider_yields_idle(self):
        ctx = _make_ctx(is_shell_prompt=True, supports_hook=False)
        decision = decide_tick(ctx)
        assert decision.transition == "idle"


class TestDecideTickIdleAndStarting:
    def test_seen_status_with_no_signal_yields_idle(self):
        ctx = _make_ctx(has_seen_status=True)
        decision = decide_tick(ctx)
        assert decision.transition == "idle"

    def test_no_signal_no_startup_yields_starting(self):
        ctx = _make_ctx(startup_time=None)
        decision = decide_tick(ctx)
        assert decision.transition == "starting"

    def test_startup_within_grace_period_yields_starting(self):
        ctx = _make_ctx(startup_time=time.monotonic())
        decision = decide_tick(ctx)
        assert decision.transition == "starting"

    def test_startup_expired_yields_idle(self):
        old_start = time.monotonic() - STARTUP_TIMEOUT - 1.0
        ctx = _make_ctx(startup_time=old_start)
        decision = decide_tick(ctx)
        assert decision.transition == "idle"


class TestDecideTickTypingGate:
    """Typing is refreshed only while output is genuinely flowing."""

    def test_active_status_with_fresh_activity_types(self):
        ctx = _make_ctx(
            resolved_status_text="Working...", last_activity_ts=time.monotonic()
        )
        decision = decide_tick(ctx)
        assert decision.transition == "active"
        assert decision.send_typing is True

    def test_active_status_with_stale_activity_does_not_type(self):
        # A long think/spinner phase: status line present, but no transcript
        # write for >60s. This is the reported "perpetual typing" bug.
        ctx = _make_ctx(
            resolved_status_text="Brewing…",
            last_activity_ts=time.monotonic() - (TYPING_MAX_QUIET + 5.0),
        )
        decision = decide_tick(ctx)
        assert decision.transition == "active"
        assert decision.send_status is True  # emoji + status bubble still update
        assert decision.send_typing is False  # but no misleading typing

    def test_active_status_with_no_activity_does_not_type(self):
        ctx = _make_ctx(resolved_status_text="Working...", last_activity_ts=None)
        decision = decide_tick(ctx)
        assert decision.transition == "active"
        assert decision.send_typing is False

    def test_typing_disabled_never_types(self):
        ctx = _make_ctx(
            resolved_status_text="Working...",
            last_activity_ts=time.monotonic(),
            typing_enabled=False,
        )
        decision = decide_tick(ctx)
        assert decision.send_typing is False

    def test_recently_active_branch_types_when_fresh(self):
        ctx = _make_ctx(is_recently_active=True, last_activity_ts=time.monotonic())
        decision = decide_tick(ctx)
        assert decision.transition == "active"
        assert decision.send_status is False
        assert decision.send_typing is True

    def test_starting_types_regardless_of_activity(self):
        # Startup grace is bounded by STARTUP_TIMEOUT; show typing immediately
        # even before the first transcript write.
        ctx = _make_ctx(startup_time=None, last_activity_ts=None)
        decision = decide_tick(ctx)
        assert decision.transition == "starting"
        assert decision.send_typing is True

    def test_starting_respects_disable_switch(self):
        ctx = _make_ctx(startup_time=None, typing_enabled=False)
        decision = decide_tick(ctx)
        assert decision.transition == "starting"
        assert decision.send_typing is False

    def test_idle_and_done_never_type(self):
        idle = decide_tick(_make_ctx(has_seen_status=True))
        assert idle.transition == "idle"
        assert idle.send_typing is False
        done = decide_tick(_make_ctx(is_shell_prompt=True, supports_hook=True))
        assert done.transition == "done"
        assert done.send_typing is False


class TestBuildStatusLine:
    def test_none_status_returns_none(self):
        assert build_status_line(None) is None

    def test_interactive_status_returns_none(self):
        status = StatusUpdate(
            raw_text="Permission?", display_label="", is_interactive=True
        )
        assert build_status_line(status) is None

    def test_multiline_passes_through_unchanged(self):
        status = StatusUpdate(raw_text="line1\nline2", display_label="")
        assert build_status_line(status) == "line1\nline2"

    def test_single_line_gets_emoji_prefix(self):
        status = StatusUpdate(raw_text="Working", display_label="")
        result = build_status_line(status)
        assert result is not None
        assert result.endswith(" Working")
        assert result != "Working"


class TestIsShellPrompt:
    @pytest.mark.parametrize(
        "command,expected",
        [
            ("bash", True),
            ("zsh", True),
            ("fish", True),
            ("sh", True),
            ("/bin/bash", True),
            ("/usr/local/bin/zsh", True),
            ("claude", False),
            ("codex", False),
            ("python3", False),
            ("", False),
        ],
        ids=[
            "bash",
            "zsh",
            "fish",
            "sh",
            "absolute_bash",
            "absolute_zsh",
            "claude",
            "codex",
            "python3",
            "empty",
        ],
    )
    def test_classification(self, command, expected):
        assert is_shell_prompt(command) is expected
