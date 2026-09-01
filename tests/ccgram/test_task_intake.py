from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ccgram.inbound_store import InboundStore
from ccgram.task_scheduler import TaskAdmission
import ccgram.handlers.task_intake as task_intake


async def test_new_task_publishes_receipt_and_prioritizes_dashboard(
    tmp_path, monkeypatch
) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    monkeypatch.setattr(task_intake, "inbound_store", store)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001),
        chat_id=-1001,
        message_id=77,
    )

    with (
        patch.object(
            task_intake.task_scheduler,
            "acquire",
            new=AsyncMock(return_value=TaskAdmission(False, False, task_id="T0042")),
        ),
        patch.object(task_intake, "record_request"),
        patch.object(task_intake, "publish_task_receipt", new=AsyncMock()) as receipt,
        patch.object(task_intake, "request_operations_dashboard_refresh") as refresh,
    ):
        admission = await task_intake.admit_request(
            window_id="@1",
            user_id=42,
            thread_id=17,
            message=message,  # type: ignore[arg-type]
            dispatch_text="check logs",
        )

    assert admission == TaskAdmission(False, False, task_id="T0042")
    receipt.assert_awaited_once_with(
        message,
        inbound_key="-1001:17:77",
        task_id="T0042",
        existing=None,
    )
    refresh.assert_called_once_with(-1001, 17)


async def test_continuation_keeps_single_task_without_new_receipt(
    tmp_path, monkeypatch
) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    monkeypatch.setattr(task_intake, "inbound_store", store)
    message = SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=78)

    with (
        patch.object(
            task_intake.task_scheduler,
            "acquire",
            new=AsyncMock(return_value=TaskAdmission(True, False, task_id="T0042")),
        ),
        patch.object(task_intake, "record_request"),
        patch.object(task_intake, "publish_task_receipt", new=AsyncMock()) as receipt,
        patch.object(task_intake, "safe_reply", new=AsyncMock()) as reply,
    ):
        admission = await task_intake.admit_request(
            window_id="@1",
            user_id=42,
            thread_id=17,
            message=message,  # type: ignore[arg-type]
            dispatch_text="supplement",
        )

    assert admission is not None and admission.continuation is True
    receipt.assert_not_awaited()
    reply.assert_awaited_once()


async def test_source_message_override_preserves_original_media_root(
    tmp_path, monkeypatch
) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    monkeypatch.setattr(task_intake, "inbound_store", store)
    confirmation = SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=900)
    with (
        patch.object(
            task_intake.task_scheduler,
            "acquire",
            new=AsyncMock(return_value=TaskAdmission(False, False, task_id="T0042")),
        ),
        patch.object(task_intake, "record_request") as record,
        patch.object(task_intake, "publish_task_receipt", new=AsyncMock()),
    ):
        admission = await task_intake.admit_request(
            window_id="@voice",
            user_id=42,
            thread_id=17,
            message=confirmation,  # type: ignore[arg-type]
            dispatch_text="voice prompt",
            source_message_id=88,
        )

    assert admission is not None
    assert "-1001:17:88" in store._items
    record.assert_called_once_with(
        "@voice",
        user_id=42,
        chat_id=-1001,
        thread_id=17,
        message_id=88,
        preserve_existing=False,
    )
