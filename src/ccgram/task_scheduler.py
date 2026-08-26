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
from typing import Literal

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
    task_id: str = ""


class TaskSupplementLimitError(RuntimeError):
    """The active operator task accepted too many unbounded supplements."""


class TaskCancellingError(RuntimeError):
    """The operator's task is waiting for cancellation confirmation."""


class TaskQueueCancelledError(RuntimeError):
    """A queued inbound message was deliberately removed before dispatch."""


CancelStatus = Literal[
    "not_found", "forbidden", "queued", "requested", "already_cancelling"
]


@dataclass(frozen=True, slots=True)
class CancelRequest:
    status: CancelStatus
    task_id: str = ""
    window_id: str = ""
    user_id: int = 0


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
    task_id: str = ""
    estimated_wait_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class TaskStats:
    active: int
    queued: int
    cancelling: int
    average_duration_seconds: int
    oldest_queue_seconds: int


@dataclass(slots=True)
class _ActiveTask:
    window_id: str
    task_id: str
    started_at: float
    touched_at: float
    started_wall: float
    touched_wall: float
    supplements: int = 0
    state: Literal["active", "cancelling"] = "active"
    cancel_requested_wall: float = 0.0
    cancel_requester_id: int = 0


@dataclass(slots=True)
class _Waiter:
    key: OperatorKey
    window_id: str
    future: asyncio.Future[TaskAdmission]
    queued_at: float
    task_id: str


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
        self._next_task_seq = 1
        self._average_duration_seconds = float(config.task_estimate_default_seconds)
        self._load_state()
        self._update_metrics()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._next_task_seq = max(1, int(raw.get("next_task_seq", 1)))
            self._average_duration_seconds = max(
                1.0,
                float(
                    raw.get(
                        "average_duration_seconds",
                        config.task_estimate_default_seconds,
                    )
                ),
            )
            now_mono = time.monotonic()
            now_wall = time.time()
            for row in raw.get("active", []):
                key = (int(row["chat_id"]), int(row["thread_id"]), int(row["user_id"]))
                touched_wall = float(row.get("touched_at", now_wall))
                remaining_age = max(0.0, now_wall - touched_wall)
                state: Literal["active", "cancelling"] = (
                    "cancelling" if row.get("state") == "cancelling" else "active"
                )
                if state == "active" and remaining_age >= config.task_lease_seconds:
                    continue
                task = _ActiveTask(
                    window_id=str(row["window_id"]),
                    task_id=str(row.get("task_id") or self._new_task_id()),
                    started_at=now_mono
                    - max(0.0, now_wall - float(row.get("started_at", now_wall))),
                    touched_at=now_mono - remaining_age,
                    started_wall=float(row.get("started_at", now_wall)),
                    touched_wall=touched_wall,
                    supplements=int(row.get("supplements", 0)),
                    state=state,
                    cancel_requested_wall=float(row.get("cancel_requested_at", 0.0)),
                    cancel_requester_id=int(row.get("cancel_requester_id", 0)),
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
                "task_id": task.task_id,
                "started_at": task.started_wall,
                "touched_at": task.touched_wall,
                "supplements": task.supplements,
                "state": task.state,
                "cancel_requested_at": task.cancel_requested_wall,
                "cancel_requester_id": task.cancel_requester_id,
            }
            for key, task in self._active.items()
        ]
        fd, temp_name = tempfile.mkstemp(prefix=".tasks-", dir=self._state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 2,
                        "next_task_seq": self._next_task_seq,
                        "average_duration_seconds": self._average_duration_seconds,
                        "active": rows,
                    },
                    handle,
                    separators=(",", ":"),
                )
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

    def _new_task_id(self) -> str:
        task_id = f"T{self._next_task_seq:04d}"
        self._next_task_seq += 1
        return task_id

    def _observe_duration(self, task: _ActiveTask, *, outcome: str) -> None:
        duration = max(0.0, time.monotonic() - task.started_at)
        TASK_DURATION.observe(duration, outcome=outcome)
        # A conservative EMA gives queue estimates useful signal without
        # allowing one unusually long task to dominate for days.
        self._average_duration_seconds = max(
            1.0, self._average_duration_seconds * 0.8 + duration * 0.2
        )

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
            if task.state == "active"
            and now - task.touched_at >= config.task_lease_seconds
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
            self._observe_duration(task, outcome="lease_expired")
        if stale:
            self._save_state()
            self._update_metrics()

    def _activate(
        self, key: OperatorKey, window_id: str, now: float, task_id: str
    ) -> None:
        wall = time.time()
        self._active[key] = _ActiveTask(window_id, task_id, now, now, wall, wall)
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
                # A generic inactivity lease is not proof that an interrupted
                # provider stopped.  Keep cancellation fail-closed until a
                # completion signal or an explicit admin force-stop arrives.
                if active.state == "cancelling":
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
                self._observe_duration(active, outcome="lease_expired")
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
                if task.state == "cancelling":
                    waiter.future.set_exception(TaskCancellingError(task.task_id))
                    continue
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
                waiter.future.set_result(
                    TaskAdmission(True, True, task_id=task.task_id)
                )
                continue
            if self._has_capacity(waiter.key):
                self._activate(waiter.key, waiter.window_id, now, waiter.task_id)
                TASK_QUEUE_WAIT.observe(max(0.0, now - waiter.queued_at))
                waiter.future.set_result(
                    TaskAdmission(False, True, task_id=waiter.task_id)
                )
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
                if active.state == "cancelling":
                    raise TaskCancellingError(active.task_id)
                if active.supplements >= config.max_task_supplements:
                    raise TaskSupplementLimitError(
                        f"task supplement limit reached ({config.max_task_supplements})"
                    )
                active.supplements += 1
                active.touched_at = now
                active.touched_wall = time.time()
                self._arm_lease(key, active.window_id)
                self._save_state()
                return TaskAdmission(
                    continuation=True, queued=False, task_id=active.task_id
                )
            if self._has_capacity(key):
                task_id = self._new_task_id()
                self._activate(key, window_id, now, task_id)
                return TaskAdmission(continuation=False, queued=False, task_id=task_id)
            future: asyncio.Future[TaskAdmission] = (
                asyncio.get_running_loop().create_future()
            )
            existing = next(
                (waiter for waiter in self._waiters if waiter.key == key), None
            )
            task_id = existing.task_id if existing else self._new_task_id()
            self._waiters.append(_Waiter(key, window_id, future, now, task_id))
            position = len(self._waiters)
            self._update_metrics()

        result = await future
        return TaskAdmission(
            continuation=result.continuation,
            queued=True,
            queue_position=position,
            task_id=result.task_id,
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
                resolved_outcome = (
                    "cancel_confirmed"
                    if active.state == "cancelling" and outcome == "done"
                    else outcome
                )
                self._observe_duration(active, outcome=resolved_outcome)
            self._save_state()
            self._update_metrics()
            self._wake_waiters(time.monotonic())
            return True

    async def request_cancel(
        self,
        *,
        chat_id: int,
        thread_id: int,
        requester_user_id: int,
        task_id: str | None = None,
        allow_any: bool = False,
    ) -> CancelRequest:
        """Cancel queued work or mark active work as awaiting confirmation."""
        normalized = task_id.upper() if task_id else None
        async with self._lock:
            waiter = next(
                (
                    candidate
                    for candidate in self._waiters
                    if candidate.key[:2] == (chat_id, thread_id)
                    and (
                        candidate.task_id.upper() == normalized
                        if normalized
                        else candidate.key[2] == requester_user_id
                    )
                ),
                None,
            )
            if waiter is not None:
                owner = waiter.key[2]
                if owner != requester_user_id and not allow_any:
                    return CancelRequest("forbidden", waiter.task_id, user_id=owner)
                matching = [
                    item
                    for item in self._waiters
                    if item.key == waiter.key and item.task_id == waiter.task_id
                ]
                self._waiters = deque(
                    item for item in self._waiters if item not in matching
                )
                for item in matching:
                    item.future.set_exception(TaskQueueCancelledError(waiter.task_id))
                self._update_metrics()
                return CancelRequest("queued", waiter.task_id, waiter.window_id, owner)

            active_match = next(
                (
                    (key, task)
                    for key, task in self._active.items()
                    if key[:2] == (chat_id, thread_id)
                    and (
                        task.task_id.upper() == normalized
                        if normalized
                        else key[2] == requester_user_id
                    )
                ),
                None,
            )
            if active_match is None:
                return CancelRequest("not_found", normalized or "")
            key, task = active_match
            if key[2] != requester_user_id and not allow_any:
                return CancelRequest("forbidden", task.task_id, task.window_id, key[2])
            if task.state == "cancelling":
                return CancelRequest(
                    "already_cancelling", task.task_id, task.window_id, key[2]
                )
            task.state = "cancelling"
            task.cancel_requested_wall = time.time()
            task.cancel_requester_id = requester_user_id
            task.touched_at = time.monotonic()
            task.touched_wall = time.time()
            self._arm_lease(key, task.window_id)
            self._save_state()
            return CancelRequest("requested", task.task_id, task.window_id, key[2])

    async def confirm_cancel(self, task_id: str, *, forced: bool = False) -> bool:
        """Release a cancelling task after the CLI stopped or was killed."""
        normalized = task_id.upper()
        async with self._lock:
            match = next(
                (
                    (key, task)
                    for key, task in self._active.items()
                    if task.task_id.upper() == normalized
                ),
                None,
            )
            if match is None:
                return False
            key, task = match
            self._active.pop(key, None)
            self._window_to_key.pop(task.window_id, None)
            lease = self._lease_tasks.pop(key, None)
            if lease is not None and lease is not asyncio.current_task():
                lease.cancel()
            self._observe_duration(
                task, outcome="force_cancelled" if forced else "cancel_confirmed"
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
            self._observe_duration(active, outcome="cancelled")
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
                state=task.state,
                age_seconds=(
                    max(0.0, time.time() - task.cancel_requested_wall)
                    if task.state == "cancelling" and task.cancel_requested_wall
                    else max(0.0, now - task.started_at)
                ),
                supplements=task.supplements,
                task_id=task.task_id,
            )
            for key, task in self._active.items()
            if (chat_id is None or key[0] == chat_id)
            and (thread_id is None or key[1] == thread_id)
        ]
        seen_queued: set[tuple[OperatorKey, str]] = set()
        for position, waiter in enumerate(self._waiters, start=1):
            key = waiter.key
            if (chat_id is not None and key[0] != chat_id) or (
                thread_id is not None and key[1] != thread_id
            ):
                continue
            group = (key, waiter.task_id)
            if group in seen_queued:
                continue
            seen_queued.add(group)
            grouped = sum(
                item.key == key and item.task_id == waiter.task_id
                for item in self._waiters
            )
            capacity = max(
                1,
                min(config.max_parallel_global, config.max_parallel_per_topic),
            )
            rounds = (position + capacity - 1) // capacity
            views.append(
                TaskView(
                    chat_id=key[0],
                    thread_id=key[1],
                    user_id=key[2],
                    window_id=waiter.window_id,
                    state="queued",
                    age_seconds=max(0.0, now - waiter.queued_at),
                    supplements=max(0, grouped - 1),
                    queue_position=position,
                    task_id=waiter.task_id,
                    estimated_wait_seconds=max(
                        1, int(rounds * self._average_duration_seconds)
                    ),
                )
            )
        return views

    def stats(self) -> TaskStats:
        views = self.views()
        queued = [view for view in views if view.state == "queued"]
        return TaskStats(
            active=sum(view.state == "active" for view in views),
            queued=len(queued),
            cancelling=sum(view.state == "cancelling" for view in views),
            average_duration_seconds=max(1, int(self._average_duration_seconds)),
            oldest_queue_seconds=max(
                (int(view.age_seconds) for view in queued), default=0
            ),
        )

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
        self._next_task_seq = 1
        self._average_duration_seconds = float(config.task_estimate_default_seconds)
        self._update_metrics()


task_scheduler = TaskScheduler(config.task_state_file)

__all__ = [
    "CancelRequest",
    "TaskAdmission",
    "TaskCancellingError",
    "TaskQueueCancelledError",
    "TaskScheduler",
    "TaskStats",
    "TaskSupplementLimitError",
    "TaskView",
    "task_scheduler",
]
