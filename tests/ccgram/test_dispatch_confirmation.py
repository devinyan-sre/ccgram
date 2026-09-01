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
            window_id="@9", provider=provider, entries=[entry]
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert tracker.record_for_window("@9").status == "accepted"  # type: ignore[union-attr]
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
