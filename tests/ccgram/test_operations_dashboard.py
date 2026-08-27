from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from telegram.error import BadRequest, Forbidden

import ccgram.thread_router as thread_router_module
from ccgram.config import config
from ccgram.operations_dashboard import DashboardTarget, OperationsDashboard
from ccgram.task_scheduler import TaskView
from ccgram.telegram_client import FakeTelegramClient
from ccgram.thread_router import ThreadRouter, thread_router


@pytest.fixture(autouse=True)
def _wire_test_router(monkeypatch) -> None:
    router = ThreadRouter(
        schedule_save=lambda: None,
        has_window_state=lambda _window_id: True,
    )
    monkeypatch.setattr(thread_router_module, "_active_router", router)


def _view(**overrides: object) -> TaskView:
    values: dict[str, object] = {
        "chat_id": -1001,
        "thread_id": 17,
        "user_id": 42,
        "window_id": "@7",
        "state": "active",
        "age_seconds": 35.0,
        "supplements": 0,
        "task_id": "T0001",
    }
    values.update(overrides)
    return TaskView(**values)  # type: ignore[arg-type]


def _configure(monkeypatch, *, scope: str = "both") -> None:
    monkeypatch.setattr(config, "group_id", -1001)
    monkeypatch.setattr(config, "dashboard_scope", scope)
    monkeypatch.setattr(config, "dashboard_max_items", 20)
    monkeypatch.setattr(config, "dashboard_completed_ttl_seconds", 180)
    monkeypatch.setattr(config, "dashboard_privacy", "normal")
    monkeypatch.setattr(config, "dashboard_pin", True)
    monkeypatch.setattr(config, "max_parallel_global", 4)
    monkeypatch.setattr(config, "max_parallel_per_topic", 2)


def test_targets_are_discovered_without_hardcoded_chat_or_topic(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch)
    thread_router.bind_thread(42, 17, "@7", "ccgram-codex-1")
    thread_router.set_group_chat_id(42, 17, -1001)
    dashboard = OperationsDashboard(FakeTelegramClient(), tmp_path / "state.json")

    targets = dashboard._targets()
    assert DashboardTarget(-1001, 1, True) in targets
    assert DashboardTarget(-1001, 17, False) in targets


def test_new_topic_is_discovered_after_dashboard_has_started(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch)
    dashboard = OperationsDashboard(FakeTelegramClient(), tmp_path / "state.json")
    assert DashboardTarget(-1001, 29, False) not in dashboard._targets()

    thread_router.bind_thread(42, 29, "@9", "new-topic-codex-1")
    thread_router.set_group_chat_id(42, 29, -1001)

    assert DashboardTarget(-1001, 29, False) in dashboard._targets()


async def test_existing_dashboard_reasserts_pin_on_first_edit(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch, scope="topic")
    client = FakeTelegramClient()
    dashboard = OperationsDashboard(client, tmp_path / "state.json")
    target = DashboardTarget(-1001, 17, False)
    dashboard._message_ids[target.key] = 55

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(target)
        await dashboard._upsert(target, force=True)

    assert client.call_count("pin_chat_message") == 1
    assert client.last_call("pin_chat_message").kwargs["message_id"] == 55  # type: ignore[union-attr]


async def test_unchanged_existing_dashboard_still_reasserts_pin(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch, scope="topic")
    client = FakeTelegramClient()
    client.set_side_effect(
        "edit_message_text", [BadRequest("Message is not modified")]
    )
    dashboard = OperationsDashboard(client, tmp_path / "state.json")
    target = DashboardTarget(-1001, 17, False)
    dashboard._message_ids[target.key] = 55

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(target)

    assert client.call_count("pin_chat_message") == 1


async def test_creates_pins_then_edits_one_message(tmp_path, monkeypatch) -> None:
    _configure(monkeypatch, scope="topic")
    client = FakeTelegramClient()
    client.returns["send_message"] = SimpleNamespace(message_id=88)
    thread_router.bind_thread(42, 17, "@7", "ccgram-codex-1")
    thread_router.set_group_chat_id(42, 17, -1001)
    dashboard = OperationsDashboard(client, tmp_path / "state.json")
    target = DashboardTarget(-1001, 17, False)

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(target)
        assert client.call_count("send_message") == 1
        assert client.call_count("pin_chat_message") == 1
        assert client.last_call("send_message").kwargs["message_thread_id"] == 17  # type: ignore[union-attr]

        # Same frame is de-duplicated instead of creating or editing spam.
        await dashboard._upsert(target)
        assert client.call_count("send_message") == 1
        assert client.call_count("edit_message_text") == 0

        # A changed task snapshot edits the original message in place.
        with patch(
            "ccgram.operations_dashboard.task_scheduler.views",
            return_value=[_view()],
        ):
            await dashboard._upsert(target)
        edit = client.last_call("edit_message_text")
        assert edit is not None
        assert edit.kwargs["message_id"] == 88
        assert "T0001" in edit.kwargs["text"]


async def test_general_uses_portable_send_without_thread_id(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch, scope="general")
    client = FakeTelegramClient()
    client.returns["send_message"] = SimpleNamespace(message_id=66)
    dashboard = OperationsDashboard(client, tmp_path / "state.json")

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(DashboardTarget(-1001, 1, True))

    sent = client.last_call("send_message")
    assert sent is not None
    assert "message_thread_id" not in sent.kwargs


async def test_deleted_dashboard_is_recreated(tmp_path, monkeypatch) -> None:
    _configure(monkeypatch, scope="topic")
    client = FakeTelegramClient()
    client.returns["send_message"] = SimpleNamespace(message_id=99)
    dashboard = OperationsDashboard(client, tmp_path / "state.json")
    target = DashboardTarget(-1001, 17, False)
    dashboard._message_ids[target.key] = 55
    client.set_side_effect(
        "edit_message_text", [BadRequest("message to edit not found")]
    )

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(target)

    assert client.call_count("edit_message_text") == 1
    assert client.call_count("send_message") == 1
    assert dashboard._message_ids[target.key] == 99


async def test_pin_permission_failure_degrades_to_editable_message(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch, scope="topic")
    client = FakeTelegramClient()
    client.returns["send_message"] = SimpleNamespace(message_id=77)
    client.set_side_effect("pin_chat_message", [Forbidden("not enough rights")])
    dashboard = OperationsDashboard(client, tmp_path / "state.json")
    target = DashboardTarget(-1001, 17, False)

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(target)

    assert dashboard._message_ids[target.key] == 77
    assert client.call_count("send_message") == 1


async def test_missing_topic_backs_off_instead_of_retrying_every_tick(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch, scope="topic")
    client = FakeTelegramClient()
    client.set_side_effect("send_message", [BadRequest("Message thread not found")])
    dashboard = OperationsDashboard(client, tmp_path / "state.json")
    target = DashboardTarget(-1001, 4268, False)

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(target)
        await dashboard._upsert(target)

    assert client.call_count("send_message") == 1
    assert dashboard._target_retry_at[target.key] > 0


async def test_missing_topic_is_persistently_quarantined_after_threshold(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch, scope="topic")
    monkeypatch.setattr(config, "dashboard_missing_topic_failures", 2)
    client = FakeTelegramClient()
    client.set_side_effect(
        "send_message",
        [
            BadRequest("Message thread not found"),
            BadRequest("Message thread not found"),
        ],
    )
    state_path = tmp_path / "state.json"
    dashboard = OperationsDashboard(client, state_path)
    target = DashboardTarget(-1001, 4268, False)

    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        await dashboard._upsert(target, force=True)
        await dashboard._upsert(target, force=True)

    assert dashboard._target_health[target.key].quarantined is True
    restored = OperationsDashboard(FakeTelegramClient(), state_path)
    assert restored._target_health[target.key].quarantined is True


def test_render_hides_prompts_and_supports_operator_privacy(
    tmp_path, monkeypatch
) -> None:
    _configure(monkeypatch)
    dashboard = OperationsDashboard(FakeTelegramClient(), tmp_path / "state.json")
    dashboard.observe_user(SimpleNamespace(id=42, username="alice", full_name="Alice"))
    target = DashboardTarget(-1001, 1, True)

    with patch(
        "ccgram.operations_dashboard.task_scheduler.views", return_value=[_view()]
    ):
        rendered = dashboard._render(target, precise_time=False)
        assert "@alice" in rendered
        assert "T0001" in rendered
        assert "prompt" not in rendered.lower()

        monkeypatch.setattr(config, "dashboard_privacy", "strict")
        strict = dashboard._render(target, precise_time=False)
        assert "@alice" not in strict
        assert dashboard._operator(42).startswith("member-")
        assert dashboard._operator(42) != "42"


def test_completed_task_is_retained_briefly(tmp_path, monkeypatch) -> None:
    _configure(monkeypatch)
    dashboard = OperationsDashboard(FakeTelegramClient(), tmp_path / "state.json")

    with patch(
        "ccgram.operations_dashboard.task_scheduler.views", return_value=[_view()]
    ):
        dashboard._capture_completions()
    with patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]):
        dashboard._capture_completions()
        rendered = dashboard._render(
            DashboardTarget(-1001, 1, True), precise_time=False
        )

    assert "T0001" in rendered
    assert "✅" in rendered


def test_render_uses_configured_beijing_time(tmp_path, monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(config, "timezone_name", "Asia/Shanghai")
    dashboard = OperationsDashboard(FakeTelegramClient(), tmp_path / "state.json")
    target = DashboardTarget(-1001, 17, False)
    fixed = datetime(2026, 8, 27, 11, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

    with (
        patch("ccgram.operations_dashboard.task_scheduler.views", return_value=[]),
        patch("ccgram.operations_dashboard.now_display", return_value=fixed),
    ):
        rendered = dashboard._render(target, precise_time=True)

    assert "11:05:00 Beijing Time" in rendered


def test_subminute_durations_use_coarse_api_safe_buckets() -> None:
    assert OperationsDashboard._duration(5) == "0s"
    assert OperationsDashboard._duration(29) == "0s"
    assert OperationsDashboard._duration(30) == "30s"
    assert OperationsDashboard._duration(59) == "30s"
