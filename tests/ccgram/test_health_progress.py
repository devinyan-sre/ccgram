"""Forward-progress health gate.

This gate decides whether the systemd watchdog heartbeat is withheld, and a
withheld heartbeat restarts production. The tests therefore lean hard on the
*false-positive* boundaries: startup before any cycle, a disabled threshold,
and progress that is merely old-but-within-budget must all stay healthy.
"""

import time

import pytest

from ccgram import bootstrap, health


@pytest.fixture(autouse=True)
def _clean_progress():
    health.reset_for_testing()
    yield
    health.reset_for_testing()


# --- progress tracking ---------------------------------------------------


def test_unknown_component_has_no_progress():
    assert health.seconds_since_progress("nope") is None


def test_record_progress_starts_the_clock():
    health.record_progress(health.STATUS_POLL)
    elapsed = health.seconds_since_progress(health.STATUS_POLL)
    assert elapsed is not None
    assert elapsed < 1.0


def test_never_reported_component_is_not_stalled():
    """Startup grace: the first cycle has not landed yet."""
    assert health.is_stalled(health.STATUS_POLL, threshold_seconds=0.0) is False


def test_fresh_progress_is_not_stalled():
    health.record_progress(health.STATUS_POLL)
    assert health.is_stalled(health.STATUS_POLL, threshold_seconds=60) is False


def test_old_progress_is_stalled(monkeypatch):
    health.record_progress(health.STATUS_POLL)
    real = time.monotonic

    monkeypatch.setattr(health.time, "monotonic", lambda: real() + 999)
    assert health.is_stalled(health.STATUS_POLL, threshold_seconds=60) is True


def test_reset_clears_all_components():
    health.record_progress(health.STATUS_POLL)
    health.record_progress(health.SESSION_MONITOR)
    health.reset_for_testing()
    assert health.seconds_since_progress(health.STATUS_POLL) is None
    assert health.seconds_since_progress(health.SESSION_MONITOR) is None


# --- the gate itself -----------------------------------------------------


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Monitor:
    def __init__(self, done: bool) -> None:
        self._task = _Task(done)


def _arm(monkeypatch, *, monitor_done=False, poll_done=False, stall=120):
    monkeypatch.setattr(bootstrap, "get_active_monitor", lambda: _Monitor(monitor_done))
    monkeypatch.setattr(bootstrap, "_status_poll_task", _Task(poll_done))
    monkeypatch.setattr(bootstrap.config, "health_stall_seconds", stall)


def test_gate_unhealthy_when_monitor_task_finished(monkeypatch):
    _arm(monkeypatch, monitor_done=True)
    assert bootstrap._runtime_healthy() is False


def test_gate_unhealthy_when_poll_task_finished(monkeypatch):
    _arm(monkeypatch, poll_done=True)
    assert bootstrap._runtime_healthy() is False


def test_gate_unhealthy_when_monitor_absent(monkeypatch):
    monkeypatch.setattr(bootstrap, "get_active_monitor", lambda: None)
    assert bootstrap._runtime_healthy() is False


def test_gate_healthy_before_first_cycle(monkeypatch):
    """The regression that matters: startup must not trip the gate."""
    _arm(monkeypatch)
    assert bootstrap._runtime_healthy() is True


def test_gate_healthy_with_fresh_progress(monkeypatch):
    _arm(monkeypatch)
    health.record_progress(health.SESSION_MONITOR)
    health.record_progress(health.STATUS_POLL)
    assert bootstrap._runtime_healthy() is True


def test_gate_catches_wedged_but_live_poll_loop(monkeypatch):
    """The whole point: tasks alive, no forward progress → unhealthy."""
    _arm(monkeypatch, stall=60)
    health.record_progress(health.SESSION_MONITOR)
    health.record_progress(health.STATUS_POLL)
    real = time.monotonic
    monkeypatch.setattr(health.time, "monotonic", lambda: real() + 999)
    assert bootstrap._runtime_healthy() is False


def test_gate_catches_wedged_session_monitor(monkeypatch):
    _arm(monkeypatch, stall=60)
    health.record_progress(health.SESSION_MONITOR)
    real = time.monotonic
    monkeypatch.setattr(health.time, "monotonic", lambda: real() + 999)
    assert bootstrap._runtime_healthy() is False


def test_zero_threshold_disables_progress_check(monkeypatch):
    """Escape hatch: liveness-only, never restarts on a progress stall."""
    _arm(monkeypatch, stall=0)
    health.record_progress(health.STATUS_POLL)
    real = time.monotonic
    monkeypatch.setattr(health.time, "monotonic", lambda: real() + 99999)
    assert bootstrap._runtime_healthy() is True
