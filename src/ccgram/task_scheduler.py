"""Fair task admission across Telegram topics and isolated operator lanes."""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time

import structlog

from .config import config
from .metrics import (
    TASK_DURATION,
    TASK_LEASE_EXPIRED,
    TASK_QUEUE_WAIT,
    TASKS_ACTIVE,
    TASKS_QUEUED,
)

logger = structlog.get_logger()

TopicKey = tuple[int, int]
OperatorKey = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class TaskAdmission:
    """Result of admitting one message to an operator lane."""

    continuation: bool
    queued: bool
    queue_position: int = 0


class TaskSupplementLimitError(RuntimeError):
    """The active operator task accepted too many unbounded supplements."""


@dataclass(frozen=True, slots=True)
class TaskView:
    chat_id: int
    thread_id: int
    user_id: int
    window_id: str
    state: str
    age_seconds: float
    supplements: int
    queue_position: int = 0


@dataclass(slots=True)
class _ActiveTask:
    window_id: str
    started_at: float
    touched_at: float
    started_wall: float
    touched_wall: float
    supplements: int = 0


@dataclass(slots=True)
class _Waiter:
    key: OperatorKey
    window_id: str
    future: asyncio.Future[TaskAdmission]
    queued_at: float


class TaskScheduler:
    """One active task per operator, with topic/global configurable caps.

    More messages from the same operator are continuations of the active task,
    not new parallel jobs. Different operators use a fair FIFO wait queue.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = asyncio.Lock()
        self._active: dict[OperatorKey, _ActiveTask] = {}
        self._window_to_key: dict[str, OperatorKey] = {}
        self._waiters: deque[_Waiter] = deque()
        self._lease_tasks: dict[OperatorKey, asyncio.Task[None]] = {}
        self._state_path = state_path
        self._load_state()
        self._update_metrics()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            now_mono = time.monotonic()
            now_wall = time.time()
            for row in raw.get("active", []):
                key = (int(row["chat_id"]), int(row["thread_id"]), int(row["user_id"]))
                touched_wall = float(row.get("touched_at", now_wall))
                remaining_age = max(0.0, now_wall - touched_wall)
                if remaining_age >= config.task_lease_seconds:
                    continue
                task = _ActiveTask(
                    window_id=str(row["window_id"]),
                    started_at=now_mono
                    - max(0.0, now_wall - float(row.get("started_at", now_wall))),
                    touched_at=now_mono - remaining_age,
                    started_wall=float(row.get("started_at", now_wall)),
                    touched_wall=touched_wall,
                    supplements=int(row.get("supplements", 0)),
                )
                self._active[key] = task
                self._window_to_key[task.window_id] = key
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not load task scheduler state", error=str(exc))

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "chat_id": key[0],
                "thread_id": key[1],
                "user_id": key[2],
                "window_id": task.window_id,
                "started_at": task.started_wall,
                "touched_at": task.touched_wall,
                "supplements": task.supplements,
            }
            for key, task in self._active.items()
        ]
        fd, temp_name = tempfile.mkstemp(prefix=".tasks-", dir=self._state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "active": rows}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self._state_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)

    def _update_metrics(self) -> None:
        TASKS_ACTIVE.set(len(self._active))
        TASKS_QUEUED.set(len(self._waiters))

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
            TASK_LEASE_EXPIRED.inc()
            TASK_DURATION.observe(
                max(0.0, now - task.started_at), outcome="lease_expired"
            )
        if stale:
            self._save_state()
            self._update_metrics()

    def _activate(self, key: OperatorKey, window_id: str, now: float) -> None:
        wall = time.time()
        self._active[key] = _ActiveTask(window_id, now, now, wall, wall)
        self._window_to_key[window_id] = key
        self._arm_lease(key, window_id)
        self._save_state()
        self._update_metrics()

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
                TASK_LEASE_EXPIRED.inc()
                TASK_DURATION.observe(
                    max(0.0, time.monotonic() - active.started_at),
                    outcome="lease_expired",
                )
                self._save_state()
                self._update_metrics()
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
                if task.supplements >= config.max_task_supplements:
                    waiter.future.set_exception(
                        TaskSupplementLimitError(
                            "task supplement limit reached "
                            f"({config.max_task_supplements})"
                        )
                    )
                    continue
                task.supplements += 1
                task.touched_at = now
                task.touched_wall = time.time()
                self._arm_lease(waiter.key, task.window_id)
                TASK_QUEUE_WAIT.observe(max(0.0, now - waiter.queued_at))
                waiter.future.set_result(TaskAdmission(True, True))
                continue
            if self._has_capacity(waiter.key):
                self._activate(waiter.key, waiter.window_id, now)
                TASK_QUEUE_WAIT.observe(max(0.0, now - waiter.queued_at))
                waiter.future.set_result(TaskAdmission(False, True))
            else:
                remaining.append(waiter)
        self._waiters = remaining
        self._save_state()
        self._update_metrics()

    async def acquire(
        self, *, chat_id: int, thread_id: int, user_id: int, window_id: str
    ) -> TaskAdmission:
        key = (chat_id, thread_id, user_id)
        async with self._lock:
            now = time.monotonic()
            self._expire_stale(now)
            self._wake_waiters(now)
            if active := self._active.get(key):
                if active.supplements >= config.max_task_supplements:
                    raise TaskSupplementLimitError(
                        f"task supplement limit reached ({config.max_task_supplements})"
                    )
                active.supplements += 1
                active.touched_at = now
                active.touched_wall = time.time()
                self._arm_lease(key, active.window_id)
                self._save_state()
                return TaskAdmission(continuation=True, queued=False)
            if self._has_capacity(key):
                self._activate(key, window_id, now)
                return TaskAdmission(continuation=False, queued=False)
            future: asyncio.Future[TaskAdmission] = (
                asyncio.get_running_loop().create_future()
            )
            self._waiters.append(_Waiter(key, window_id, future, now))
            position = len(self._waiters)
            self._update_metrics()

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

    async def release_window(self, window_id: str, *, outcome: str = "done") -> bool:
        async with self._lock:
            key = self._window_to_key.pop(window_id, None)
            if key is None:
                return False
            active = self._active.pop(key, None)
            lease = self._lease_tasks.pop(key, None)
            if lease is not None and lease is not asyncio.current_task():
                lease.cancel()
            if active is not None:
                TASK_DURATION.observe(
                    max(0.0, time.monotonic() - active.started_at), outcome=outcome
                )
            self._save_state()
            self._update_metrics()
            self._wake_waiters(time.monotonic())
            return True

    async def cancel_operator(
        self, *, chat_id: int, thread_id: int, user_id: int
    ) -> str | None:
        """Cancel a queued or active operator task; return its window if active."""
        key = (chat_id, thread_id, user_id)
        async with self._lock:
            queued = [waiter for waiter in self._waiters if waiter.key == key]
            if queued:
                self._waiters = deque(
                    waiter for waiter in self._waiters if waiter.key != key
                )
                for waiter in queued:
                    waiter.future.cancel()
                self._update_metrics()
                return ""
            active = self._active.pop(key, None)
            if active is None:
                return None
            self._window_to_key.pop(active.window_id, None)
            lease = self._lease_tasks.pop(key, None)
            if lease is not None and lease is not asyncio.current_task():
                lease.cancel()
            TASK_DURATION.observe(
                max(0.0, time.monotonic() - active.started_at), outcome="cancelled"
            )
            self._save_state()
            self._update_metrics()
            self._wake_waiters(time.monotonic())
            return active.window_id

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
                self._update_metrics()
                return True
        return False

    def snapshot(self) -> tuple[int, int]:
        return len(self._active), len(self._waiters)

    def views(
        self, *, chat_id: int | None = None, thread_id: int | None = None
    ) -> list[TaskView]:
        now = time.monotonic()
        views = [
            TaskView(
                chat_id=key[0],
                thread_id=key[1],
                user_id=key[2],
                window_id=task.window_id,
                state="active",
                age_seconds=max(0.0, now - task.started_at),
                supplements=task.supplements,
            )
            for key, task in self._active.items()
            if (chat_id is None or key[0] == chat_id)
            and (thread_id is None or key[1] == thread_id)
        ]
        for position, waiter in enumerate(self._waiters, start=1):
            key = waiter.key
            if (chat_id is not None and key[0] != chat_id) or (
                thread_id is not None and key[1] != thread_id
            ):
                continue
            views.append(
                TaskView(
                    chat_id=key[0],
                    thread_id=key[1],
                    user_id=key[2],
                    window_id=waiter.window_id,
                    state="queued",
                    age_seconds=max(0.0, now - waiter.queued_at),
                    supplements=0,
                    queue_position=position,
                )
            )
        return views

    def start_recovered_leases(self) -> None:
        """Arm lease timers for active rows restored before the event loop."""
        for key, task in tuple(self._active.items()):
            self._arm_lease(key, task.window_id)

    def reset_for_testing(self) -> None:
        for waiter in self._waiters:
            waiter.future.cancel()
        for lease in self._lease_tasks.values():
            lease.cancel()
        self._active.clear()
        self._window_to_key.clear()
        self._waiters.clear()
        self._lease_tasks.clear()
        self._update_metrics()


task_scheduler = TaskScheduler(config.task_state_file)

__all__ = [
    "TaskAdmission",
    "TaskScheduler",
    "TaskSupplementLimitError",
    "TaskView",
    "task_scheduler",
]
