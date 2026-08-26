from unittest.mock import patch

from ccgram.inbound_store import InboundStore


def test_claim_then_stage_and_persist(tmp_path) -> None:
    path = tmp_path / "inbound.json"
    store = InboundStore(path)

    assert store.claim_message(chat_id=-100, thread_id=7, message_id=9) is True
    assert store.claim_message(chat_id=-100, thread_id=7, message_id=9) is False
    assert (
        store.stage(
            chat_id=-100,
            thread_id=7,
            user_id=10,
            message_id=9,
            window_id="@1",
            text="deploy",
        )
        is True
    )
    assert (
        store.stage(
            chat_id=-100,
            thread_id=7,
            user_id=10,
            message_id=9,
            window_id="@1",
            text="deploy",
        )
        is False
    )

    restored = InboundStore(path)
    assert [item.text for item in restored.recoverable()] == ["deploy"]


def test_dispatching_is_never_recovered_automatically(tmp_path) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    assert store.stage(
        chat_id=-100,
        thread_id=7,
        user_id=10,
        message_id=9,
        window_id="@1",
        text="dangerous operation",
    )
    store.set_state("-100:7:9", "dispatching")

    interrupted = store.interrupt_ambiguous()

    assert [item.message_id for item in interrupted] == [9]
    assert store.recoverable() == []


def test_completed_records_are_pruned_after_retention(tmp_path) -> None:
    store = InboundStore(tmp_path / "inbound.json")
    assert store.stage(
        chat_id=-100,
        thread_id=7,
        user_id=10,
        message_id=9,
        window_id="@1",
        text="done",
    )
    store.mark_window_done("@1")
    with patch("ccgram.inbound_store.config.inbound_dedupe_hours", 1):
        store._items["-100:7:9"].updated_at = 0
        store.claim_message(chat_id=-100, thread_id=7, message_id=10)
    assert "-100:7:9" not in store._items
