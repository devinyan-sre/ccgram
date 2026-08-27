from pathlib import Path

from ccgram.config import config
from ccgram.delivery_outbox import delivery_outbox
from ccgram.handlers.messaging_pipeline.message_task import ContentTask
from ccgram.metrics import render


async def test_outbox_persists_until_ack(tmp_path: Path) -> None:
    old = config.outbox_file
    config.outbox_file = tmp_path / "outbox.json"
    delivery_outbox.reset_for_testing()
    try:
        task = ContentTask(
            window_id="@1",
            parts=("hello",),
            thread_id=22,
            session_id="s1",
            delivery_id="d1",
        )
        assert await delivery_outbox.add(task, 7) is True
        assert await delivery_outbox.add(task, 7) is False
        assert delivery_outbox.has_pending_session("s1") is True
        assert delivery_outbox.pending_tasks() == [(7, task)]

        await delivery_outbox.advance("d1", 1)
        await delivery_outbox.delivered("d1")
        assert delivery_outbox.has_pending_session("s1") is False
        assert await delivery_outbox.add(task, 7) is False
    finally:
        config.outbox_file = old
        delivery_outbox.reset_for_testing()


async def test_outbox_survives_reload(tmp_path: Path) -> None:
    old = config.outbox_file
    config.outbox_file = tmp_path / "outbox.json"
    delivery_outbox.reset_for_testing()
    try:
        task = ContentTask(
            window_id="@2",
            parts=("pending",),
            thread_id=33,
            session_id="s2",
            delivery_id="d2",
        )
        await delivery_outbox.add(task, 8)
        delivery_outbox.reset_for_testing()
        assert delivery_outbox.pending_tasks() == [(8, task)]
    finally:
        config.outbox_file = old
        delivery_outbox.reset_for_testing()


def test_empty_outbox_load_reconciles_pending_gauge(tmp_path: Path) -> None:
    old = config.outbox_file
    config.outbox_file = tmp_path / "missing.json"
    delivery_outbox.reset_for_testing()
    try:
        assert delivery_outbox.snapshot() == (0, 0)
        assert 'ccgram_outbox_items{state="pending"} 0' in render()
        assert 'ccgram_outbox_items{state="retrying"} 0' in render()
    finally:
        config.outbox_file = old
        delivery_outbox.reset_for_testing()
