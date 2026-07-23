from __future__ import annotations

from types import SimpleNamespace

import pytest

from ccgram.config import config
from ccgram.operator_alerts import (
    ErrorRateTracker,
    PermissionCheckResult,
    _can_manage_topics,
    check_group_permission,
    check_group_permissions,
    error_signature,
    format_error_alert,
    format_missing_permission_alert,
    notify_operator,
    resolve_operator_chat_id,
)
from ccgram.telegram_client import FakeTelegramClient


@pytest.fixture
def _restore_config():
    saved = (config.operator_chat_id, set(config.allowed_users))
    yield
    config.operator_chat_id, config.allowed_users = saved[0], saved[1]


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
        client = FakeTelegramClient()
        client.set_side_effect("send_message", [RuntimeError("boom")])
        assert await notify_operator(client, "hi") is False


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
