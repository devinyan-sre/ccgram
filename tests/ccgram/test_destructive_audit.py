"""Tests for the destructive-action audit trail and operator alerting.

The behaviour under test is the fix for the 2026-07-25 blind spot: unattended
destruction used to log at ``info`` and never alert, so a silent single event
went unnoticed for days. The contract now is — always logged at ``warning`` and
counted, DM'd only when nobody asked for it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from ccgram import destructive_audit
from ccgram.destructive_audit import (
    ACTION_TOPIC_RETIRED,
    ACTION_WINDOW_KILLED_BY_USER,
    ACTION_WINDOW_KILLED_UNBOUND,
    ACTOR_AUTO,
    ACTOR_USER,
    OUTCOME_EXECUTED,
    OUTCOME_SKIPPED_SUSPENDED,
    DestructiveAction,
    describe_action,
    format_destructive_alert,
    record_destructive,
    set_audit_client,
)


@pytest.fixture(autouse=True)
def _reset_sink():
    set_audit_client(None)
    yield
    set_audit_client(None)


class TestDescribeAction:
    def test_known_action_renders_a_sentence(self) -> None:
        assert describe_action(ACTION_WINDOW_KILLED_UNBOUND).startswith("Window killed")

    def test_unknown_action_passes_through(self) -> None:
        assert describe_action("something_new") == "something_new"


class TestFormatDestructiveAlert:
    def test_includes_action_detail_and_context(self) -> None:
        text = format_destructive_alert(
            DestructiveAction(
                action=ACTION_WINDOW_KILLED_UNBOUND,
                actor=ACTOR_AUTO,
                detail="unsaved work gone",
                window_id="@7",
                thread_id=42,
            )
        )
        assert "Window killed" in text
        assert "unsaved work gone" in text
        assert "@7" in text
        assert "42" in text

    def test_omits_context_line_when_no_ids(self) -> None:
        text = format_destructive_alert(
            DestructiveAction(action=ACTION_TOPIC_RETIRED, actor=ACTOR_AUTO)
        )
        assert "window" not in text
        assert "topic `" not in text


class TestRecordDestructive:
    async def test_auto_action_dms_the_operator(self) -> None:
        set_audit_client(AsyncMock())
        with (
            patch(
                "ccgram.operator_alerts.notify_operator", new_callable=AsyncMock
            ) as n,
            patch("ccgram.destructive_audit.config") as cfg,
        ):
            cfg.destructive_alerts_enabled = True
            await record_destructive(
                ACTION_WINDOW_KILLED_UNBOUND, actor=ACTOR_AUTO, window_id="@7"
            )
        n.assert_awaited_once()

    async def test_user_action_is_never_dmd(self) -> None:
        """The user is looking at what they just confirmed — alerting is noise."""
        set_audit_client(AsyncMock())
        with (
            patch(
                "ccgram.operator_alerts.notify_operator", new_callable=AsyncMock
            ) as n,
            patch("ccgram.destructive_audit.config") as cfg,
        ):
            cfg.destructive_alerts_enabled = True
            await record_destructive(
                ACTION_WINDOW_KILLED_BY_USER, actor=ACTOR_USER, window_id="@7"
            )
        n.assert_not_awaited()

    async def test_alerts_can_be_disabled(self) -> None:
        set_audit_client(AsyncMock())
        with (
            patch(
                "ccgram.operator_alerts.notify_operator", new_callable=AsyncMock
            ) as n,
            patch("ccgram.destructive_audit.config") as cfg,
        ):
            cfg.destructive_alerts_enabled = False
            await record_destructive(ACTION_WINDOW_KILLED_UNBOUND, actor=ACTOR_AUTO)
        n.assert_not_awaited()

    async def test_unarmed_sink_does_not_raise(self) -> None:
        """Destruction before bootstrap (or after shutdown) must still record."""
        with patch("ccgram.destructive_audit.config") as cfg:
            cfg.destructive_alerts_enabled = True
            await record_destructive(ACTION_WINDOW_KILLED_UNBOUND, actor=ACTOR_AUTO)

    async def test_always_logs_at_warning_and_counts(self) -> None:
        """Audit log + metric run even when the DM is switched off."""
        with (
            patch("ccgram.destructive_audit.logger") as mock_logger,
            patch("ccgram.destructive_audit.DESTRUCTIVE_ACTIONS") as mock_counter,
            patch("ccgram.destructive_audit.config") as cfg,
        ):
            cfg.destructive_alerts_enabled = False
            await record_destructive(
                ACTION_TOPIC_RETIRED, actor=ACTOR_AUTO, thread_id=9
            )

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.kwargs["audit"] == "destructive"
        assert mock_logger.warning.call_args.kwargs["action"] == ACTION_TOPIC_RETIRED
        mock_counter.inc.assert_called_once_with(
            action=ACTION_TOPIC_RETIRED, actor=ACTOR_AUTO, outcome=OUTCOME_EXECUTED
        )

    async def test_skipped_action_is_recorded_but_not_dmd(self) -> None:
        """A suppressed action is worth auditing; the breaker sends its own DM."""
        set_audit_client(AsyncMock())
        with (
            patch(
                "ccgram.operator_alerts.notify_operator", new_callable=AsyncMock
            ) as n,
            patch("ccgram.destructive_audit.DESTRUCTIVE_ACTIONS") as mock_counter,
            patch("ccgram.destructive_audit.config") as cfg,
        ):
            cfg.destructive_alerts_enabled = True
            await record_destructive(
                ACTION_TOPIC_RETIRED,
                actor=ACTOR_AUTO,
                outcome=OUTCOME_SKIPPED_SUSPENDED,
            )
        n.assert_not_awaited()
        mock_counter.inc.assert_called_once_with(
            action=ACTION_TOPIC_RETIRED,
            actor=ACTOR_AUTO,
            outcome=OUTCOME_SKIPPED_SUSPENDED,
        )

    async def test_warning_level_cannot_trip_error_burst_alerting(self) -> None:
        """`maybe_alert_error` only reacts to error/critical — no double-fire."""
        from ccgram.operator_alerts import maybe_alert_error

        assert maybe_alert_error("warning", "destructive_action", now=0.0) == 0


class TestDestructiveActionModel:
    @pytest.mark.parametrize(
        ("actor", "expected"), [(ACTOR_AUTO, True), (ACTOR_USER, False)]
    )
    def test_is_unattended(self, actor: str, expected: bool) -> None:
        event = DestructiveAction(action=ACTION_TOPIC_RETIRED, actor=actor)
        assert event.is_unattended is expected


class TestAuditModuleIsSelfContained:
    def test_module_exposes_the_documented_surface(self) -> None:
        for name in ("record_destructive", "set_audit_client", "DestructiveAction"):
            assert hasattr(destructive_audit, name)
