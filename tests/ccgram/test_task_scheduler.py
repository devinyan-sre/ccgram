import asyncio
import json
import time
from unittest.mock import patch

import pytest

from ccgram.task_scheduler import (
    TaskCancellingError,
    TaskQueueCancelledError,
    TaskScheduler,
    TaskSupplementLimitError,
)

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
    assert first.task_id == supplement.task_id == "T0001"
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


async def test_queued_supplements_share_one_visible_task_and_eta() -> None:
    scheduler = TaskScheduler()
    with (
        patch(f"{_CFG}.max_parallel_per_topic", 1),
        patch(f"{_CFG}.max_parallel_global", 1),
        patch(f"{_CFG}.task_estimate_default_seconds", 42),
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
        queued = [view for view in scheduler.views() if view.state == "queued"]

    assert len(queued) == 1
    assert queued[0].task_id == "T0002"
    assert queued[0].supplements == 1
    assert queued[0].estimated_wait_seconds == 300
    await scheduler.cancel_operator(chat_id=-100, thread_id=7, user_id=10)
    await asyncio.gather(*waits, return_exceptions=True)


async def test_active_cancel_waits_for_confirmation_before_releasing_slot() -> None:
    scheduler = TaskScheduler()
    admission = await scheduler.acquire(
        chat_id=-100, thread_id=7, user_id=10, window_id="@1"
    )

    request = await scheduler.request_cancel(
        chat_id=-100,
        thread_id=7,
        requester_user_id=10,
        task_id=admission.task_id,
    )

    assert request.status == "requested"
    assert scheduler.snapshot() == (1, 0)
    assert scheduler.views()[0].state == "cancelling"
    with pytest.raises(TaskCancellingError):
        await scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")

    assert await scheduler.confirm_cancel(admission.task_id) is True
    assert scheduler.snapshot() == (0, 0)


async def test_cancelling_task_is_never_released_by_generic_lease() -> None:
    scheduler = TaskScheduler()
    admission = await scheduler.acquire(
        chat_id=-100, thread_id=7, user_id=10, window_id="@1"
    )
    await scheduler.request_cancel(
        chat_id=-100,
        thread_id=7,
        requester_user_id=10,
        task_id=admission.task_id,
    )

    scheduler._expire_stale(time.monotonic() + 86400)

    assert scheduler.snapshot() == (1, 0)
    assert scheduler.views()[0].state == "cancelling"


async def test_precise_cancel_enforces_owner_and_admin_override() -> None:
    scheduler = TaskScheduler()
    own = await scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
    other = await scheduler.acquire(
        chat_id=-100, thread_id=7, user_id=20, window_id="@2"
    )

    denied = await scheduler.request_cancel(
        chat_id=-100,
        thread_id=7,
        requester_user_id=10,
        task_id=other.task_id,
    )
    allowed = await scheduler.request_cancel(
        chat_id=-100,
        thread_id=7,
        requester_user_id=10,
        task_id=other.task_id,
        allow_any=True,
    )

    assert own.task_id != other.task_id
    assert denied.status == "forbidden"
    assert allowed.status == "requested"


async def test_precise_queued_cancel_unwinds_without_cancelling_handler() -> None:
    scheduler = TaskScheduler()
    with (
        patch(f"{_CFG}.max_parallel_per_topic", 1),
        patch(f"{_CFG}.max_parallel_global", 1),
    ):
        await scheduler.acquire(
            chat_id=-100, thread_id=7, user_id=99, window_id="@busy"
        )
        waiting = asyncio.create_task(
            scheduler.acquire(chat_id=-100, thread_id=7, user_id=10, window_id="@1")
        )
        await asyncio.sleep(0)
        task_id = next(
            view.task_id for view in scheduler.views() if view.state == "queued"
        )
        result = await scheduler.request_cancel(
            chat_id=-100,
            thread_id=7,
            requester_user_id=10,
            task_id=task_id,
        )

    assert result.status == "queued"
    with pytest.raises(TaskQueueCancelledError):
        await waiting
    assert waiting.cancelled() is False


async def test_task_id_sequence_survives_restart(tmp_path) -> None:
    path = tmp_path / "tasks.json"
    scheduler = TaskScheduler(path)
    first = await scheduler.acquire(
        chat_id=-100, thread_id=7, user_id=10, window_id="@1"
    )

    restored = TaskScheduler(path)
    second = await restored.acquire(
        chat_id=-100, thread_id=8, user_id=20, window_id="@2"
    )

    assert first.task_id == "T0001"
    assert restored.views(chat_id=-100, thread_id=7)[0].task_id == "T0001"
    assert second.task_id == "T0002"


async def test_cancelling_state_survives_restart_past_normal_lease(tmp_path) -> None:
    path = tmp_path / "tasks.json"
    scheduler = TaskScheduler(path)
    admission = await scheduler.acquire(
        chat_id=-100, thread_id=7, user_id=10, window_id="@1"
    )
    await scheduler.request_cancel(
        chat_id=-100,
        thread_id=7,
        requester_user_id=10,
        task_id=admission.task_id,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["active"][0]["touched_at"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = TaskScheduler(path)

    assert restored.views()[0].state == "cancelling"
