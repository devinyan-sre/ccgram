from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ccgram.inbound_store import InboundStore
from ccgram.telegram_client import FakeTelegramClient
import ccgram.task_receipts as task_receipts


async def test_publish_receipt_persists_telegram_message_id(
    tmp_path, monkeypatch
) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    assert store.stage(
        chat_id=-100,
        thread_id=7,
        user_id=10,
        message_id=9,
        window_id="@1",
        text="check logs",
    )
    monkeypatch.setattr(task_receipts, "inbound_store", store)
    source = SimpleNamespace()
    sent = SimpleNamespace(message_id=88)
    with patch.object(
        task_receipts, "safe_reply", new=AsyncMock(return_value=sent)
    ) as reply:
        result = await task_receipts.publish_task_receipt(
            source,  # type: ignore[arg-type]
            inbound_key="-100:7:9",
            task_id="T0042",
        )

    assert result is sent
    reply.assert_awaited_once()
    call = reply.await_args
    assert call is not None and "T0042" in call.args[1]
    assert store.receipt_refs_for_window("@1") == [("-100:7:9", -100, 7, 88)]


async def test_terminal_window_deletes_persisted_receipt(tmp_path, monkeypatch) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    assert store.stage(
        chat_id=-100,
        thread_id=7,
        user_id=10,
        message_id=9,
        window_id="@1",
        text="check logs",
    )
    assert store.set_receipt("-100:7:9", 88)
    client = FakeTelegramClient()
    monkeypatch.setattr(task_receipts, "inbound_store", store)
    task_receipts.set_receipt_client(client)

    store.mark_window_done("@1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert client.call_count("delete_message") == 1
    assert client.last_call("delete_message").kwargs == {  # type: ignore[union-attr]
        "chat_id": -100,
        "message_id": 88,
    }
    assert store.receipt_refs_for_window("@1") == []
    task_receipts.reset_for_testing()


async def test_restart_resumes_cleanup_for_terminal_receipt(
    tmp_path, monkeypatch
) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    assert store.stage(
        chat_id=-100,
        thread_id=7,
        user_id=10,
        message_id=9,
        window_id="@1",
        text="check logs",
    )
    assert store.set_receipt("-100:7:9", 88)
    store.mark_window_done("@1")
    client = FakeTelegramClient()
    monkeypatch.setattr(task_receipts, "inbound_store", store)

    task_receipts.set_receipt_client(client)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert client.call_count("delete_message") == 1
    assert store.receipt_refs_for_window("@1") == []
    task_receipts.reset_for_testing()
