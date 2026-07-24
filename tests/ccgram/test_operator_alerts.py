from __future__ import annotations

from types import SimpleNamespace

import pytest

from ccgram.config import config
from ccgram.operator_alerts import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    ErrorRateTracker,
    PermissionCheckResult,
    _can_manage_topics,
    check_group_permission,
    check_group_permissions,
    check_operator_reachable,
    error_signature,
    format_error_alert,
    format_missing_permission_alert,
    notify_operator,
    resolve_operator_chat_id,
    resolve_operator_fallback_chat_id,
)
from ccgram.telegram_client import FakeTelegramClient


class TestAlertSeverityMetrics:
    """Every alert outcome must be countable, including 'nobody to alert'."""

    def _rendered(self):
        from ccgram import metrics

        return metrics.registry.render()

    async def test_successful_alert_counts_as_sent(self, _restore_config):
        config.operator_chat_id = 777
        config.allowed_users = {777}
        config.operator_fallback_chat_id = None
        config.group_id = None
        client = FakeTelegramClient()

        assert await notify_operator(client, "hi", severity=SEVERITY_CRITICAL) is True
        assert 'severity="critical",outcome="sent"' in self._rendered()

    async def test_unconfigured_sink_is_counted_not_silent(self, _restore_config):
        """A deployment that can't alert anyone must not look like 'no alerts'."""
        config.operator_chat_id = None
        config.allowed_users = set()
        config.operator_fallback_chat_id = None
        config.group_id = None
        client = FakeTelegramClient()

        assert await notify_operator(client, "hi", severity=SEVERITY_WARNING) is False
        assert 'severity="warning",outcome="no_sink"' in self._rendered()

    async def test_fallback_delivery_is_distinguishable_from_primary(
        self, _restore_config
    ):
        config.operator_chat_id = 777
        config.allowed_users = {777}
        config.operator_fallback_chat_id = -100
        config.group_id = -100
        client = FakeTelegramClient()
        client.set_side_effect("send_message", [RuntimeError("can't initiate"), None])

        assert await notify_operator(client, "hi", severity=SEVERITY_INFO) is True
        assert 'severity="info",outcome="sent_fallback"' in self._rendered()

    async def test_default_severity_is_warning(self, _restore_config):
        config.operator_chat_id = 777
        config.allowed_users = {777}
        config.operator_fallback_chat_id = None
        config.group_id = None
        client = FakeTelegramClient()

        await notify_operator(client, "hi")
        assert 'severity="warning",outcome="sent"' in self._rendered()


@pytest.fixture
def _restore_config():
    saved = (
        config.operator_chat_id,
        set(config.allowed_users),
        config.operator_fallback_chat_id,
        config.group_id,
    )
    yield
    (
        config.operator_chat_id,
        config.allowed_users,
        config.operator_fallback_chat_id,
        config.group_id,
    ) = (saved[0], saved[1], saved[2], saved[3])


class TestResolveOperatorChatId:
    def test_explicit_override_wins(self, _restore_config):
        config.operator_chat_id = 999
        config.allowed_users = {5, 3, 8}
        assert resolve_operator_chat_id() == 999

    def test_falls_back_to_lowest_allowed_user(self, _restore_config):
        config.operator_chat_id = None
        config.allowed_users = {5, 3, 8}
        assert resolve_operator_chat_id() == 3

    def test_none_when_no_operator(self, _restore_config):
        config.operator_chat_id = None
        config.allowed_users = set()
        assert resolve_operator_chat_id() is None


class TestCanManageTopics:
    def test_creator_always_true(self):
        assert _can_manage_topics(SimpleNamespace(status="creator")) is True

    def test_admin_with_right(self):
        m = SimpleNamespace(status="administrator", can_manage_topics=True)
        assert _can_manage_topics(m) is True

    def test_admin_without_right(self):
        m = SimpleNamespace(status="administrator", can_manage_topics=False)
        assert _can_manage_topics(m) is False

    def test_plain_member_false(self):
        assert _can_manage_topics(SimpleNamespace(status="member")) is False


class TestCheckGroupPermission:
    async def test_ok_when_admin_can_manage(self):
        client = FakeTelegramClient()
        client.returns["get_chat_member"] = SimpleNamespace(
            status="administrator", can_manage_topics=True
        )
        result = await check_group_permission(client, -100, bot_id=42)
        assert result.ok is True
        assert result.can_manage_topics is True

    async def test_not_ok_when_missing_right(self):
        client = FakeTelegramClient()
        client.returns["get_chat_member"] = SimpleNamespace(
            status="administrator", can_manage_topics=False
        )
        result = await check_group_permission(client, -100, bot_id=42)
        assert result.ok is False

    async def test_api_error_is_swallowed(self):
        client = FakeTelegramClient()
        client.set_side_effect("get_chat_member", [RuntimeError("not in chat")])
        result = await check_group_permission(client, -100, bot_id=42)
        assert result.ok is False
        assert "not in chat" in result.reason


class TestCheckGroupPermissions:
    async def test_dms_operator_on_missing_right(self, _restore_config):
        config.operator_chat_id = 777
        config.allowed_users = {777}
        client = FakeTelegramClient()
        client.returns["get_chat_member"] = SimpleNamespace(
            status="administrator", can_manage_topics=False
        )
        results = await check_group_permissions(client, [-100], bot_id=42)
        assert results[0].ok is False
        sent = client.last_call("send_message")
        assert sent is not None
        assert sent.kwargs["chat_id"] == 777

    async def test_no_dm_when_all_ok(self, _restore_config):
        config.operator_chat_id = 777
        config.allowed_users = {777}
        client = FakeTelegramClient()
        client.returns["get_chat_member"] = SimpleNamespace(status="creator")
        await check_group_permissions(client, [-100], bot_id=42)
        assert client.call_count("send_message") == 0


class TestNotifyOperator:
    async def test_skips_when_no_operator(self, _restore_config):
        config.operator_chat_id = None
        config.allowed_users = set()
        client = FakeTelegramClient()
        assert await notify_operator(client, "hi") is False
        assert client.call_count("send_message") == 0

    async def test_send_failure_returns_false(self, _restore_config):
        config.operator_chat_id = 5
        config.allowed_users = {5}
        config.operator_fallback_chat_id = None
        config.group_id = None
        client = FakeTelegramClient()
        client.set_side_effect("send_message", [RuntimeError("boom")])
        assert await notify_operator(client, "hi") is False


class TestResolveOperatorFallbackChatId:
    def test_explicit_fallback_wins(self, _restore_config):
        config.operator_fallback_chat_id = -100
        config.group_id = -200
        assert resolve_operator_fallback_chat_id() == -100

    def test_falls_back_to_group_id(self, _restore_config):
        config.operator_fallback_chat_id = None
        config.group_id = -200
        assert resolve_operator_fallback_chat_id() == -200

    def test_none_when_neither_set(self, _restore_config):
        config.operator_fallback_chat_id = None
        config.group_id = None
        assert resolve_operator_fallback_chat_id() is None


class TestNotifyOperatorFallback:
    async def test_falls_back_to_group_when_dm_fails(self, _restore_config):
        config.operator_chat_id = 5
        config.allowed_users = {5}
        config.operator_fallback_chat_id = None
        config.group_id = -200
        client = FakeTelegramClient()
        # Primary DM (chat 5) fails; fallback group (-200) succeeds (None).
        client.set_side_effect(
            "send_message", [RuntimeError("can't initiate conversation"), None]
        )
        assert await notify_operator(client, "hi") is True
        assert client.call_count("send_message") == 2
        sent = client.last_call("send_message")
        assert sent is not None
        assert sent.kwargs["chat_id"] == -200

    async def test_returns_false_when_all_sinks_fail(self, _restore_config):
        config.operator_chat_id = 5
        config.allowed_users = {5}
        config.operator_fallback_chat_id = -200
        config.group_id = None
        client = FakeTelegramClient()
        client.set_side_effect(
            "send_message", [RuntimeError("boom"), RuntimeError("boom")]
        )
        assert await notify_operator(client, "hi") is False
        assert client.call_count("send_message") == 2

    async def test_no_duplicate_sink_when_primary_equals_fallback(
        self, _restore_config
    ):
        config.operator_chat_id = -200
        config.allowed_users = {-200}
        config.operator_fallback_chat_id = -200
        config.group_id = -200
        client = FakeTelegramClient()
        client.set_side_effect("send_message", [RuntimeError("boom")])
        assert await notify_operator(client, "hi") is False
        # Same id de-duplicated: only one send attempt, no pointless retry.
        assert client.call_count("send_message") == 1


class TestCheckOperatorReachable:
    async def test_true_when_get_chat_succeeds(self, _restore_config):
        config.operator_chat_id = 5
        config.allowed_users = {5}
        client = FakeTelegramClient()
        assert await check_operator_reachable(client) is True

    async def test_false_and_skips_when_no_operator(self, _restore_config):
        config.operator_chat_id = None
        config.allowed_users = set()
        client = FakeTelegramClient()
        assert await check_operator_reachable(client) is False
        assert client.call_count("get_chat") == 0

    async def test_posts_notice_to_fallback_when_unreachable(self, _restore_config):
        config.operator_chat_id = 5
        config.allowed_users = {5}
        config.operator_fallback_chat_id = None
        config.group_id = -200
        client = FakeTelegramClient()
        client.set_side_effect("get_chat", [RuntimeError("chat not found")])
        assert await check_operator_reachable(client) is False
        sent = client.last_call("send_message")
        assert sent is not None
        assert sent.kwargs["chat_id"] == -200

    async def test_no_notice_when_no_distinct_fallback(self, _restore_config):
        config.operator_chat_id = 5
        config.allowed_users = {5}
        config.operator_fallback_chat_id = None
        config.group_id = None
        client = FakeTelegramClient()
        client.set_side_effect("get_chat", [RuntimeError("chat not found")])
        assert await check_operator_reachable(client) is False
        assert client.call_count("send_message") == 0


class TestFormatters:
    def test_permission_alert_mentions_chat_and_manage_topics(self):
        result = PermissionCheckResult(
            chat_id=-100500, ok=False, can_manage_topics=False
        )
        text = format_missing_permission_alert(result)
        assert "-100500" in text
        assert "Manage Topics" in text or "管理话题" in text

    def test_error_alert_has_count_and_message(self):
        text = format_error_alert(7, 60.0, "Failed to create topic")
        assert "7" in text
        assert "Failed to create topic" in text


class TestErrorRateTracker:
    def test_no_alert_below_threshold(self):
        tr = ErrorRateTracker(threshold=3, window_seconds=60)
        assert tr.record("x", 0.0) == 0
        assert tr.record("x", 1.0) == 0

    def test_alerts_at_threshold(self):
        tr = ErrorRateTracker(threshold=3, window_seconds=60)
        tr.record("x", 0.0)
        tr.record("x", 1.0)
        assert tr.record("x", 2.0) == 3

    def test_old_events_evicted_from_window(self):
        tr = ErrorRateTracker(threshold=3, window_seconds=10)
        tr.record("x", 0.0)
        tr.record("x", 1.0)
        # 20s later the first two are outside the window
        assert tr.record("x", 20.0) == 0

    def test_cooldown_suppresses_repeat_alert(self):
        tr = ErrorRateTracker(threshold=2, window_seconds=60, cooldown_seconds=600)
        tr.record("x", 0.0)
        assert tr.record("x", 1.0) == 2  # first alert
        # fresh burst but inside cooldown
        tr.record("x", 2.0)
        assert tr.record("x", 3.0) == 0

    def test_distinct_signatures_independent(self):
        tr = ErrorRateTracker(threshold=2, window_seconds=60)
        tr.record("a", 0.0)
        tr.record("b", 0.0)
        assert tr.record("a", 1.0) == 2
        assert tr.record("b", 1.0) == 2


class TestErrorSignature:
    def test_strips_trailing_ids(self):
        a = error_signature("Failed to create topic for window @32 in chat -100")
        b = error_signature("Failed to create topic for window @5 in chat -200")
        assert a == b

    def test_falls_back_for_leading_digits(self):
        assert error_signature("500 server error") == "500 server error"[:80]


class TestMaybeAlertError:
    def setup_method(self):
        from ccgram import operator_alerts

        operator_alerts.reset_error_alerts_for_testing()

    def teardown_method(self):
        from ccgram import operator_alerts

        operator_alerts.reset_error_alerts_for_testing()

    def test_ignores_non_error_levels(self):
        from ccgram import operator_alerts

        operator_alerts.set_error_alert_client(FakeTelegramClient())
        for i in range(20):
            assert operator_alerts.maybe_alert_error("info", "boom", now=float(i)) == 0

    def test_no_alert_when_unarmed(self):
        from ccgram import operator_alerts

        operator_alerts.set_error_alert_client(None)
        for i in range(20):
            assert operator_alerts.maybe_alert_error("error", "boom", now=float(i)) == 0

    async def test_alerts_operator_on_burst(self, _restore_config):
        from ccgram import operator_alerts

        config.operator_chat_id = 42
        config.allowed_users = {42}
        client = FakeTelegramClient()
        operator_alerts.set_error_alert_client(client)
        # default threshold 5 within 60s
        counts = [
            operator_alerts.maybe_alert_error("error", "disk on fire", now=float(i))
            for i in range(5)
        ]
        assert counts[-1] == 5
        # DM is scheduled on the loop via call_soon_threadsafe — yield to run it.
        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client.call_count("send_message") == 1

    def test_processor_is_passthrough(self):
        from ccgram import operator_alerts

        operator_alerts.set_error_alert_client(None)
        d = {"event": "x", "level": "error"}
        assert operator_alerts.error_alert_processor(None, "error", d) is d
