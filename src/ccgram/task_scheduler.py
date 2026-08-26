"""Fair task admission across Telegram topics and isolated operator lanes."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import time

import structlog

from .config import config

logger = structlog.get_logger()

TopicKey = tuple[int, int]
OperatorKey = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class TaskAdmission:
    """Result of admitting one message to an operator lane."""

    continuation: bool
    queued: bool
    queue_position: int = 0


@dataclass(slots=True)
class _ActiveTask:
    window_id: str
    started_at: float
    touched_at: float


@dataclass(slots=True)
class _Waiter:
    key: OperatorKey
    window_id: str
    future: asyncio.Future[TaskAdmission]


class TaskScheduler:
    """One active task per operator, with topic/global configurable caps.

    More messages from the same operator are continuations of the active task,
    not new parallel jobs. Different operators use a fair FIFO wait queue.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: dict[OperatorKey, _ActiveTask] = {}
        self._window_to_key: dict[str, OperatorKey] = {}
        self._waiters: deque[_Waiter] = deque()
        self._lease_tasks: dict[OperatorKey, asyncio.Task[None]] = {}

    @staticmethod
    def _topic(key: OperatorKey) -> TopicKey:
        return key[0], key[1]

    def _topic_active_count(self, topic: TopicKey) -> int:
        return sum(self._topic(key) == topic for key in self._active)

    def _has_capacity(self, key: OperatorKey) -> bool:
        return (
            len(self._active) < config.max_parallel_global
            and self._topic_active_count(self._topic(key))
            < config.max_parallel_per_topic
        )

    def _expire_stale(self, now: float) -> None:
        stale = [
            key
            for key, task in self._active.items()
            if now - task.touched_at >= config.task_lease_seconds
        ]
        for key in stale:
            task = self._active.pop(key)
            self._window_to_key.pop(task.window_id, None)
            lease = self._lease_tasks.pop(key, None)
            if lease is not None and lease is not asyncio.current_task():
                lease.cancel()
            logger.warning(
                "Expired stale task lease",
                chat_id=key[0],
                thread_id=key[1],
                user_id=key[2],
                window_id=task.window_id,
            )

    def _activate(self, key: OperatorKey, window_id: str, now: float) -> None:
        self._active[key] = _ActiveTask(window_id, now, now)
        self._window_to_key[window_id] = key
        self._arm_lease(key, window_id)

    def _arm_lease(self, key: OperatorKey, window_id: str) -> None:
        previous = self._lease_tasks.pop(key, None)
        if previous is not None and previous is not asyncio.current_task():
            previous.cancel()
        self._lease_tasks[key] = asyncio.create_task(
            self._expire_lease(key, window_id),
            name=f"task-lease:{key[0]}:{key[1]}:{key[2]}",
        )

    async def _expire_lease(self, key: OperatorKey, window_id: str) -> None:
        try:
            await asyncio.sleep(config.task_lease_seconds)
            async with self._lock:
                active = self._active.get(key)
                if active is None or active.window_id != window_id:
                    return
                self._active.pop(key, None)
                self._window_to_key.pop(window_id, None)
                self._lease_tasks.pop(key, None)
                logger.warning(
                    "Task lease timer expired",
                    chat_id=key[0],
                    thread_id=key[1],
                    user_id=key[2],
                    window_id=window_id,
                )
                self._wake_waiters(time.monotonic())
        except asyncio.CancelledError:
            return

    def _wake_waiters(self, now: float) -> None:
        """Admit the oldest eligible waiters without head-of-line blocking."""
        remaining: deque[_Waiter] = deque()
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.future.cancelled():
                continue
            if waiter.key in self._active:
                task = self._active[waiter.key]
                task.touched_at = now
                self._arm_lease(waiter.key, task.window_id)
                waiter.future.set_result(TaskAdmission(True, True))
                continue
            if self._has_capacity(waiter.key):
                self._activate(waiter.key, waiter.window_id, now)
                waiter.future.set_result(TaskAdmission(False, True))
            else:
                remaining.append(waiter)
        self._waiters = remaining

    async def acquire(
        self, *, chat_id: int, thread_id: int, user_id: int, window_id: str
    ) -> TaskAdmission:
        key = (chat_id, thread_id, user_id)
        async with self._lock:
            now = time.monotonic()
            self._expire_stale(now)
            self._wake_waiters(now)
            if active := self._active.get(key):
                active.touched_at = now
                self._arm_lease(key, active.window_id)
                return TaskAdmission(continuation=True, queued=False)
            if self._has_capacity(key):
                self._activate(key, window_id, now)
                return TaskAdmission(continuation=False, queued=False)
            future: asyncio.Future[TaskAdmission] = (
                asyncio.get_running_loop().create_future()
            )
            self._waiters.append(_Waiter(key, window_id, future))
            position = len(self._waiters)

        result = await future
        return TaskAdmission(
            continuation=result.continuation,
            queued=True,
            queue_position=position,
        )

    async def queue_position(
        self, *, chat_id: int, thread_id: int, user_id: int
    ) -> int:
        key = (chat_id, thread_id, user_id)
        async with self._lock:
            for position, waiter in enumerate(self._waiters, start=1):
                if waiter.key == key:
                    return position
        return 0

    async def release_window(self, window_id: str) -> bool:
        async with self._lock:
            key = self._window_to_key.pop(window_id, None)
            if key is None:
                return False
            self._active.pop(key, None)
            lease = self._lease_tasks.pop(key, None)
            if lease is not None and lease is not asyncio.current_task():
                lease.cancel()
            self._wake_waiters(time.monotonic())
            return True

    async def cancel_waiter(
        self, *, chat_id: int, thread_id: int, user_id: int
    ) -> bool:
        key = (chat_id, thread_id, user_id)
        async with self._lock:
            for waiter in tuple(self._waiters):
                if waiter.key != key:
                    continue
                self._waiters.remove(waiter)
                waiter.future.cancel()
                return True
        return False

    def snapshot(self) -> tuple[int, int]:
        return len(self._active), len(self._waiters)

    def reset_for_testing(self) -> None:
        for waiter in self._waiters:
            waiter.future.cancel()
        for lease in self._lease_tasks.values():
            lease.cancel()
        self._active.clear()
        self._window_to_key.clear()
        self._waiters.clear()
        self._lease_tasks.clear()


task_scheduler = TaskScheduler()

__all__ = ["TaskAdmission", "TaskScheduler", "task_scheduler"]
