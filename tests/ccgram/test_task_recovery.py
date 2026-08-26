import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.inbound_store import InboundItem
from ccgram import task_recovery
from ccgram.task_recovery import recover_tasks


async def test_recovery_schedules_queued_work_without_blocking_bootstrap(
    monkeypatch,
) -> None:
    item = InboundItem(
        key="-100:7:9",
        chat_id=-100,
        thread_id=7,
        user_id=10,
        message_id=9,
        window_id="@1",
        text="recover me",
        state="queued",
        created_at=1.0,
        updated_at=1.0,
    )
    blocked = asyncio.Event()

    async def slow_recovery(_item: InboundItem) -> None:
        await blocked.wait()

    router = MagicMock()
    router.get_window_for_thread.return_value = "@1"
    mux = MagicMock()
    mux.find_window_by_id = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(task_recovery, "thread_router", router)
    monkeypatch.setattr(task_recovery, "multiplexer", mux)
    with (
        patch("ccgram.task_recovery.task_scheduler.start_recovered_leases"),
        patch(
            "ccgram.task_recovery.inbound_store.interrupt_ambiguous", return_value=[]
        ),
        patch("ccgram.task_recovery.inbound_store.recoverable", return_value=[item]),
        patch("ccgram.task_recovery._recover_one", side_effect=slow_recovery),
    ):
        scheduled, interrupted = await asyncio.wait_for(
            recover_tasks(MagicMock()), timeout=0.2
        )

    assert (scheduled, interrupted) == (1, 0)
    blocked.set()
    await asyncio.sleep(0)
