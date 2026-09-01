"""Core read/write facade for durable explicit task-lane routing."""

from __future__ import annotations

from .thread_router import thread_router


def find_task_lane(
    *, chat_id: int, thread_id: int, user_id: int, task_id: str
) -> str | None:
    return thread_router.task_lane_for_id(
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        task_id=task_id,
    )


def clear_task_lane(window_id: str) -> None:
    thread_router.clear_task_lane(window_id)


__all__ = ["clear_task_lane", "find_task_lane"]
