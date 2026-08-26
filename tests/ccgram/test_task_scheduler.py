import asyncio
from unittest.mock import patch

import pytest

from ccgram.task_scheduler import TaskScheduler, TaskSupplementLimitError

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


async def test_active_task_survives_scheduler_restart(tmp_path) -> None:
    path = tmp_path / "tasks.json"
    scheduler = TaskScheduler(path)
    await scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")

    restored = TaskScheduler(path)

    views = restored.views(chat_id=-100, thread_id=7)
    assert len(views) == 1
    assert views[0].window_id == "@1"
    assert views[0].state == "active"


async def test_supplement_limit_is_enforced() -> None:
    scheduler = TaskScheduler()
    with patch(f"{_CFG}.max_task_supplements", 1):
        await scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
        assert (
            await scheduler.acquire(
                chat_id=-100, thread_id=7, user_id=10, window_id="@1"
            )
        ).continuation
        with pytest.raises(TaskSupplementLimitError):
            await scheduler.acquire(
                chat_id=-100, thread_id=7, user_id=10, window_id="@1"
            )


async def test_same_operator_queued_messages_become_ordered_supplements() -> None:
    scheduler = TaskScheduler()
    with (
        patch(f"{_CFG}.max_parallel_per_topic", 1),
        patch(f"{_CFG}.max_parallel_global", 1),
        patch(f"{_CFG}.max_task_supplements", 2),
    ):
        await scheduler.acquire(
            chat_id=-100, thread_id=8, user_id=99, window_id="@busy"
        )
        first = asyncio.create_task(
            scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
        )
        second = asyncio.create_task(
            scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
        )
        await asyncio.sleep(0)
        await scheduler.release_window("@busy")
        first_result, second_result = await asyncio.gather(first, second)

    assert first_result.continuation is False
    assert second_result.continuation is True
    assert scheduler.views()[0].supplements == 1


async def test_cancel_operator_removes_all_of_their_queued_messages() -> None:
    scheduler = TaskScheduler()
    with (
        patch(f"{_CFG}.max_parallel_per_topic", 1),
        patch(f"{_CFG}.max_parallel_global", 1),
    ):
        await scheduler.acquire(
            chat_id=-100, thread_id=8, user_id=99, window_id="@busy"
        )
        waits = [
            asyncio.create_task(
                scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
            )
            for _ in range(2)
        ]
        await asyncio.sleep(0)
        assert (
            await scheduler.cancel_operator(chat_id=-100, thread_id=7, user_id=10) == ""
        )

    await asyncio.gather(*waits, return_exceptions=True)
    assert all(task.cancelled() for task in waits)
    assert scheduler.snapshot() == (1, 0)
