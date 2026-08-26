import asyncio
from unittest.mock import patch

from ccgram.task_scheduler import TaskScheduler

_CFG = "ccgram.task_scheduler.config"


async def test_same_operator_message_is_a_continuation() -> None:
    scheduler = TaskScheduler()
    with (
        patch(f"{_CFG}.max_parallel_per_topic", 2),
        patch(f"{_CFG}.max_parallel_global", 4),
    ):
        first = await scheduler.acquire(
            chat_id=-100, thread_id=7, user_id=10, window_id="@1"
        )
        supplement = await scheduler.acquire(
            chat_id=-100, thread_id=7, user_id=10, window_id="@1"
        )

    assert first.continuation is False
    assert supplement.continuation is True
    assert scheduler.snapshot() == (1, 0)


async def test_topic_limit_queues_third_operator_until_release() -> None:
    scheduler = TaskScheduler()
    with (
        patch(f"{_CFG}.max_parallel_per_topic", 2),
        patch(f"{_CFG}.max_parallel_global", 4),
    ):
        await scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
        await scheduler.acquire(chat_id=-100, thread_id=7, user_id=20, window_id="@2")
        waiting = asyncio.create_task(
            scheduler.acquire(chat_id=-100, thread_id=7, user_id=30, window_id="@3")
        )
        await asyncio.sleep(0)
        assert not waiting.done()
        assert (
            await scheduler.queue_position(chat_id=-100, thread_id=7, user_id=30) == 1
        )

        assert await scheduler.release_window("@1") is True
        admitted = await asyncio.wait_for(waiting, timeout=1)

    assert admitted.queued is True
    assert admitted.continuation is False


async def test_global_limit_applies_across_topics() -> None:
    scheduler = TaskScheduler()
    with (
        patch(f"{_CFG}.max_parallel_per_topic", 2),
        patch(f"{_CFG}.max_parallel_global", 2),
    ):
        await scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
        await scheduler.acquire(chat_id=-100, thread_id=8, user_id=20, window_id="@2")
        waiting = asyncio.create_task(
            scheduler.acquire(chat_id=-100, thread_id=9, user_id=30, window_id="@3")
        )
        await asyncio.sleep(0)
        assert not waiting.done()
        await scheduler.release_window("@2")
        await asyncio.wait_for(waiting, timeout=1)

    assert scheduler.snapshot() == (2, 0)
