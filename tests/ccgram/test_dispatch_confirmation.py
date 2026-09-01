from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ccgram.config import config
from ccgram.dispatch_confirmation import DispatchConfirmation
from ccgram.providers.claude import ClaudeProvider
from ccgram.providers.codex import CodexProvider
from ccgram.providers.gemini import GeminiProvider
from ccgram.providers.pi import PiProvider
from ccgram.task_scheduler import task_scheduler


def _begin(tracker: DispatchConfirmation) -> None:
    tracker.begin(
        task_id="T0001",
        chat_id=-100,
        thread_id=42,
        user_id=7,
        window_id="@9",
        provider="codex",
    )


@pytest.mark.parametrize(
    ("provider", "entry"),
    [
        (ClaudeProvider(), {"type": "user", "message": {"content": "hello"}}),
        (
            CodexProvider(),
            {
                "type": "response_item",
                "payload": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
        ),
        (GeminiProvider(), {"type": "user", "content": "hello"}),
        (PiProvider(), {"type": "user", "message": {"role": "user"}}),
    ],
)
async def test_all_transcript_providers_acknowledge_real_user_turn(
    tmp_path, provider, entry
) -> None:
    tracker = DispatchConfirmation(tmp_path / "dispatch.json")
    tracker._update_receipt = AsyncMock()  # type: ignore[method-assign]
    _begin(tracker)
    with (
        patch.object(task_scheduler, "retry_stuck", new=AsyncMock()),
        patch.object(task_scheduler, "set_phase", new=AsyncMock()),
    ):
        await tracker.mark_written("@9")
        tracker.observe_transcript_entries(
            window_id="@9", provider=provider, entries=[entry], start_offset=123
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert tracker.record_for_window("@9").status == "accepted"  # type: ignore[union-attr]
    assert tracker.record_for_window("@9").transcript_offset == 123  # type: ignore[union-attr]
    tracker.reset_for_testing()


async def test_transcript_offset_survives_restart(tmp_path) -> None:
    path = tmp_path / "dispatch.json"
    tracker = DispatchConfirmation(path)
    _begin(tracker)
    entry = {
        "type": "response_item",
        "payload": {
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    }
    tracker.observe_transcript_entries(
        window_id="@9",
        provider=CodexProvider(),
        entries=[entry],
        start_offset=456,
    )

    restored = DispatchConfirmation(path)

    assert restored.record_for_window("@9").transcript_offset == 456  # type: ignore[union-attr]
    tracker.reset_for_testing()
    restored.reset_for_testing()


async def test_continue_wait_extends_lease_without_touching_progress(tmp_path) -> None:
    tracker = DispatchConfirmation(tmp_path / "dispatch.json")
    tracker._update_receipt = AsyncMock()  # type: ignore[method-assign]
    _begin(tracker)
    record = tracker._records["@9"]
    record.status = "slow"
    record.last_progress_at = 100.0
    with patch.object(task_scheduler, "extend_lease", new=AsyncMock()) as extend:
        result = await tracker.continue_waiting("T0001", requester_user_id=7)

    assert result == "waiting"
    assert tracker.record_for_window("@9").last_progress_at == 100.0  # type: ignore[union-attr]
    extend.assert_awaited_once_with("@9")
    assert tracker._update_receipt.await_args.kwargs["controls"] == "slow"  # type: ignore[attr-defined]
    tracker.reset_for_testing()


async def test_check_status_confirms_missing_cli_window(tmp_path) -> None:
    tracker = DispatchConfirmation(tmp_path / "dispatch.json")
    tracker._mark_stuck = AsyncMock()  # type: ignore[method-assign]
    _begin(tracker)
    tracker._records["@9"].status = "slow"
    with patch(
        "ccgram.dispatch_confirmation.multiplexer",
        new=SimpleNamespace(find_window_by_id=AsyncMock(return_value=None)),
    ):
        status, text = await tracker.check_status("T0001", requester_user_id=7)

    assert status == "missing"
    assert "CLI 窗口不存在" in text
    tracker._mark_stuck.assert_awaited_once()  # type: ignore[attr-defined]
    tracker.reset_for_testing()


async def test_slow_warning_is_explicit_and_offers_three_controls(
    tmp_path, monkeypatch
) -> None:
    tracker = DispatchConfirmation(tmp_path / "dispatch.json")
    _begin(tracker)
    record = tracker._records["@9"]
    record.status = "accepted"
    record.last_progress_at = 1.0
    monkeypatch.setattr(config, "task_progress_warn_seconds", 0.01)
    update = AsyncMock()
    with (
        patch.object(task_scheduler, "set_phase", new=AsyncMock()),
        patch("ccgram.task_receipts.update_task_receipts", update),
    ):
        tracker._arm_progress_watchdog("@9")
        await asyncio.sleep(0.03)

    assert tracker.record_for_window("@9").status == "slow"  # type: ignore[union-attr]
    assert update.await_args is not None
    text = update.await_args.args[1]
    markup = update.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]
    assert "可能停滞" in text
    assert callbacks == ["dc:s:T0001", "dc:w:T0001", "dc:c:T0001"]
    tracker.reset_for_testing()


async def test_ack_timeout_retries_only_enter_then_fails_closed(
    tmp_path, monkeypatch
) -> None:
    tracker = DispatchConfirmation(tmp_path / "dispatch.json")
    tracker._update_receipt = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(config, "dispatch_ack_seconds", 0.01)
    monkeypatch.setattr(config, "dispatch_retry_count", 1)
    _begin(tracker)
    with (
        patch(
            "ccgram.dispatch_confirmation.multiplexer",
            new=SimpleNamespace(send_keys=AsyncMock(return_value=True)),
        ) as mux,
        patch.object(task_scheduler, "set_phase", new=AsyncMock()),
        patch.object(task_scheduler, "mark_stuck", new=AsyncMock()),
    ):
        await tracker.mark_written("@9")
        await asyncio.sleep(0.04)
    mux.send_keys.assert_awaited_once_with("@9", "Enter", enter=False, literal=False)
    assert tracker.record_for_window("@9").status == "stuck"  # type: ignore[union-attr]
    tracker.reset_for_testing()


async def test_unresolved_submit_blocks_only_same_operator_lane(tmp_path) -> None:
    tracker = DispatchConfirmation(tmp_path / "dispatch.json")
    _begin(tracker)
    assert (
        tracker.unresolved_for_operator(
            chat_id=-100, thread_id=42, user_id=7, window_id="@9"
        )
        is not None
    )
    assert (
        tracker.unresolved_for_operator(
            chat_id=-100, thread_id=42, user_id=8, window_id="@10"
        )
        is None
    )
    tracker.reset_for_testing()


async def test_restart_turns_ambiguous_submit_into_stuck_state(tmp_path) -> None:
    path = tmp_path / "dispatch.json"
    before = DispatchConfirmation(path)
    _begin(before)
    before._update_receipt = AsyncMock()  # type: ignore[method-assign]
    with patch.object(task_scheduler, "set_phase", new=AsyncMock()):
        await before.mark_written("@9")
    before._cancel_watchdog("@9")

    after = DispatchConfirmation(path)
    after._mark_stuck = AsyncMock()  # type: ignore[method-assign]
    after.start(SimpleNamespace())  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert after.record_for_window("@9").status == "stuck"  # type: ignore[union-attr]
    after._mark_stuck.assert_awaited_once()  # type: ignore[attr-defined]
    after.reset_for_testing()
