"""Tests for the mass-death circuit breaker.

The scenario being defended against is the 2026-07-25 incident: a tmux server
restart killed six windows at 09:48:15 and the autoclose sweep destroyed four
topics at 10:05 — roughly seventeen minutes later. A breaker that recovers
faster than that gap is useless, so the suspension outlasting the detection
window is part of the contract, not a tuning detail.
"""

from unittest.mock import AsyncMock, patch

import pytest

from ccgram.destructive_guard import (
    BLOCKED_MASS_DEATH,
    destruction_blocked,
    format_breaker_alert,
    note_window_death,
    note_window_death_and_alert,
    reset_for_testing,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_testing()
    yield
    reset_for_testing()


@pytest.fixture
def cfg():
    with patch("ccgram.destructive_guard.config") as mock_config:
        mock_config.mass_death_threshold = 3
        mock_config.mass_death_window_seconds = 120
        mock_config.mass_death_suspend_minutes = 30
        yield mock_config


class TestTripping:
    def test_below_threshold_does_not_trip(self, cfg) -> None:
        assert note_window_death(now=0.0) is False
        assert note_window_death(now=1.0) is False
        assert destruction_blocked(now=2.0) is None

    def test_threshold_trips_and_blocks(self, cfg) -> None:
        trips = [note_window_death(now=float(i)) for i in range(3)]
        assert trips == [False, False, True]
        assert destruction_blocked(now=5.0) == BLOCKED_MASS_DEATH

    def test_deaths_spread_beyond_the_window_do_not_trip(self, cfg) -> None:
        """Three deaths over an hour are three users, not an outage."""
        for i in range(3):
            assert note_window_death(now=i * 200.0) is False
        assert destruction_blocked(now=600.0) is None

    def test_suspension_outlasts_the_incident_gap(self, cfg) -> None:
        """The real incident destroyed topics ~17 min after the deaths."""
        for i in range(3):
            note_window_death(now=float(i))
        seventeen_minutes = 17 * 60.0
        assert destruction_blocked(now=seventeen_minutes) == BLOCKED_MASS_DEATH

    def test_suspension_eventually_lapses(self, cfg) -> None:
        for i in range(3):
            note_window_death(now=float(i))
        assert destruction_blocked(now=31 * 60.0) is None

    def test_trip_alert_fires_once_per_episode(self, cfg) -> None:
        """A burst of ten deaths must not become ten operator DMs."""
        trips = [note_window_death(now=float(i)) for i in range(10)]
        assert trips.count(True) == 1

    def test_new_episode_after_recovery_trips_again(self, cfg) -> None:
        for i in range(3):
            note_window_death(now=float(i))
        later = 40 * 60.0
        trips = [note_window_death(now=later + i) for i in range(3)]
        assert trips.count(True) == 1


class TestDisabled:
    def test_threshold_zero_disables_everything(self, cfg) -> None:
        cfg.mass_death_threshold = 0
        for i in range(50):
            assert note_window_death(now=float(i)) is False
        assert destruction_blocked(now=1.0) is None


class TestAlerting:
    async def test_alert_sent_on_trip(self, cfg) -> None:
        with (
            patch(
                "ccgram.destructive_audit.get_audit_client", return_value=AsyncMock()
            ),
            patch(
                "ccgram.operator_alerts.notify_operator", new_callable=AsyncMock
            ) as notify,
        ):
            for i in range(3):
                await note_window_death_and_alert(now=float(i))
        notify.assert_awaited_once()

    async def test_no_alert_below_threshold(self, cfg) -> None:
        with (
            patch(
                "ccgram.destructive_audit.get_audit_client", return_value=AsyncMock()
            ),
            patch(
                "ccgram.operator_alerts.notify_operator", new_callable=AsyncMock
            ) as notify,
        ):
            await note_window_death_and_alert(now=0.0)
        notify.assert_not_awaited()

    async def test_unarmed_sink_does_not_raise(self, cfg) -> None:
        with patch("ccgram.destructive_audit.get_audit_client", return_value=None):
            for i in range(3):
                await note_window_death_and_alert(now=float(i))

    def test_alert_text_states_the_consequence(self, cfg) -> None:
        text = format_breaker_alert(6)
        assert "6" in text
        assert "30" in text
        assert "suspended" in text.lower()
