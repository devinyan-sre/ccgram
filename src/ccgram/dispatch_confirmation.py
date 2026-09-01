"""Fail-closed confirmation that a provider CLI accepted an inbound prompt.

Writing bytes to tmux/herdr is not proof that an agent TUI submitted them. This
module keeps a durable per-window state machine, observes provider transcript
user-turn events, retries only the submit key, and blocks a second prompt from
being merged into an unresolved input buffer.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Literal

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .config import config
from .metrics import (
    DISPATCH_ACK_TIMEOUTS,
    DISPATCH_RETRIES,
    MESSAGE_MERGES_PREVENTED,
)
from .multiplexer import multiplexer
from .operations_dashboard import request_operations_dashboard_refresh
from .task_scheduler import task_scheduler
from .telegram_client import TelegramClient
from .utils import task_done_callback

logger = structlog.get_logger()

DispatchStatus = Literal["received", "submitting", "accepted", "slow", "stuck"]
CB_DISPATCH_RETRY_PREFIX = "dc:r:"
CB_DISPATCH_CANCEL_PREFIX = "dc:c:"
CB_DISPATCH_CHECK_PREFIX = "dc:s:"
CB_DISPATCH_WAIT_PREFIX = "dc:w:"


@dataclass(slots=True)
class DispatchRecord:
    task_id: str
    chat_id: int
    thread_id: int
    user_id: int
    window_id: str
    provider: str
    status: DispatchStatus
    created_at: float
    updated_at: float
    written_at: float = 0.0
    accepted_at: float = 0.0
    last_progress_at: float = 0.0
    transcript_offset: int = 0
    warning_snoozed_until: float = 0.0
    retry_count: int = 0

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "DispatchRecord":
        status = str(row.get("status", "stuck"))
        if status not in ("received", "submitting", "accepted", "slow", "stuck"):
            status = "stuck"
        return cls(
            task_id=str(row["task_id"]),
            chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]),
            user_id=int(row["user_id"]),
            window_id=str(row["window_id"]),
            provider=str(row.get("provider", "unknown")),
            status=status,  # type: ignore[arg-type]
            created_at=float(row.get("created_at", 0.0)),
            updated_at=float(row.get("updated_at", 0.0)),
            written_at=float(row.get("written_at", 0.0)),
            accepted_at=float(row.get("accepted_at", 0.0)),
            last_progress_at=float(row.get("last_progress_at", 0.0)),
            transcript_offset=max(0, int(row.get("transcript_offset", 0))),
            warning_snoozed_until=float(row.get("warning_snoozed_until", 0.0)),
            retry_count=max(0, int(row.get("retry_count", 0))),
        )


class DispatchConfirmation:
    """Durable dispatch state plus acknowledgement/progress watchdogs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._records: dict[str, DispatchRecord] = {}
        self._watchdogs: dict[str, asyncio.Task[None]] = {}
        self._client: TelegramClient | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for row in raw.get("records", []):
                if isinstance(row, dict):
                    record = DispatchRecord.from_dict(row)
                    self._records[record.window_id] = record
        except OSError, ValueError, TypeError, KeyError:
            logger.warning("Could not load dispatch confirmation state", exc_info=True)

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".dispatch-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 2,
                        "records": [asdict(row) for row in self._records.values()],
                    },
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)

    def start(self, client: TelegramClient) -> None:
        """Attach Telegram and fail closed on ambiguous pre-restart submits."""
        self._client = client
        for snapshot in self.records():
            if snapshot.status in ("received", "submitting"):
                with self._lock:
                    record = self._records.get(snapshot.window_id)
                    if record is None:
                        continue
                    record.status = "stuck"
                    record.updated_at = time.time()
                    self._save_locked()
                self._spawn(self._mark_stuck(record, reason="restart"))
            elif snapshot.status == "stuck":
                self._spawn(self._show_stuck(snapshot, reason="restart"))
            else:
                self._arm_progress_watchdog(snapshot.window_id)

    def begin(
        self,
        *,
        task_id: str,
        chat_id: int,
        thread_id: int,
        user_id: int,
        window_id: str,
        provider: str,
    ) -> None:
        """Record a new root task before any terminal write can race its ack."""
        self._cancel_watchdog(window_id)
        now = time.time()
        record = DispatchRecord(
            task_id=task_id,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            window_id=window_id,
            provider=provider,
            status="received",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._records[window_id] = record
            self._save_locked()

    def _persist(self, record: DispatchRecord) -> None:
        with self._lock:
            current = self._records.get(record.window_id)
            if current is record:
                self._save_locked()

    def records(self) -> list[DispatchRecord]:
        with self._lock:
            return [
                DispatchRecord.from_dict(asdict(row)) for row in self._records.values()
            ]

    def record_for_window(self, window_id: str) -> DispatchRecord | None:
        with self._lock:
            row = self._records.get(window_id)
            return DispatchRecord.from_dict(asdict(row)) if row is not None else None

    def record_for_task(self, task_id: str) -> DispatchRecord | None:
        normalized = task_id.upper()
        with self._lock:
            row = next(
                (
                    item
                    for item in self._records.values()
                    if item.task_id.upper() == normalized
                ),
                None,
            )
            return DispatchRecord.from_dict(asdict(row)) if row is not None else None

    def unresolved_for_operator(
        self, *, chat_id: int, thread_id: int, user_id: int, window_id: str
    ) -> DispatchRecord | None:
        with self._lock:
            row = self._records.get(window_id)
            if (
                row is not None
                and row.chat_id == chat_id
                and row.thread_id == thread_id
                and row.user_id == user_id
                and row.status in ("received", "submitting", "stuck")
            ):
                MESSAGE_MERGES_PREVENTED.inc()
                return DispatchRecord.from_dict(asdict(row))
        return None

    async def mark_written(self, window_id: str) -> bool:
        with self._lock:
            record = self._records.get(window_id)
            if record is None:
                return False
            if record.status == "accepted":
                return True
            record.status = "submitting"
            record.written_at = time.time()
            record.updated_at = record.written_at
            self._save_locked()
        await task_scheduler.set_phase(window_id, "submitting")
        await self._update_receipt(
            record,
            f"⌨️ 正在提交 · 任务 {record.task_id} · 等待 {record.provider} 确认",
        )
        self._arm_ack_watchdog(window_id)
        return True

    def observe_transcript_entries(
        self,
        *,
        window_id: str,
        provider: Any,
        entries: list[dict[str, Any]],
        start_offset: int = 0,
    ) -> None:
        """Accept a task on a real provider user-turn; otherwise record progress."""
        if not window_id or not entries:
            return
        with self._lock:
            record = self._records.get(window_id)
            if record is None:
                return
        user_turn = any(provider.is_user_transcript_entry(entry) for entry in entries)
        if user_turn and start_offset >= 0:
            with self._lock:
                current = self._records.get(window_id)
                if current is not None:
                    current.transcript_offset = start_offset
                    current.updated_at = time.time()
                    self._save_locked()
        if user_turn and record.status in ("submitting", "stuck"):
            self._spawn(self.acknowledge(window_id, evidence="transcript_user_turn"))
        elif record.status in ("accepted", "slow"):
            self.touch(window_id)

    async def acknowledge(self, window_id: str, *, evidence: str) -> bool:
        with self._lock:
            record = self._records.get(window_id)
            if record is None:
                return False
            now = time.time()
            record.status = "accepted"
            record.accepted_at = record.accepted_at or now
            record.last_progress_at = now
            record.warning_snoozed_until = 0.0
            record.updated_at = now
            self._save_locked()
        self._cancel_watchdog(window_id)
        await task_scheduler.retry_stuck(window_id)
        await task_scheduler.set_phase(window_id, "analysis")
        await self._update_receipt(
            record,
            f"🔵 已开始 · 任务 {record.task_id} · {record.provider} 已确认",
        )
        request_operations_dashboard_refresh(record.chat_id, record.thread_id)
        logger.info(
            "Provider dispatch acknowledged",
            task_id=record.task_id,
            window_id=window_id,
            provider=record.provider,
            evidence=evidence,
            latency_seconds=max(0, int(now - (record.written_at or record.created_at))),
        )
        self._arm_progress_watchdog(window_id)
        return True

    def touch(self, window_id: str) -> None:
        with self._lock:
            record = self._records.get(window_id)
            if record is None or record.status not in ("accepted", "slow"):
                return
            was_slow = record.status == "slow"
            record.status = "accepted"
            record.last_progress_at = time.time()
            record.updated_at = record.last_progress_at
            record.warning_snoozed_until = 0.0
            self._save_locked()
        if was_slow:
            self._spawn(
                self._update_receipt(
                    record,
                    f"🔵 已恢复进展 · 任务 {record.task_id} · {record.provider} 处理中",
                )
            )
        self._arm_progress_watchdog(window_id)

    def complete(self, window_id: str) -> None:
        self._cancel_watchdog(window_id)
        with self._lock:
            if self._records.pop(window_id, None) is not None:
                self._save_locked()

    async def continue_waiting(self, task_id: str, *, requester_user_id: int) -> str:
        """Snooze the no-progress warning without fabricating provider activity."""
        with self._lock:
            record = next(
                (
                    row
                    for row in self._records.values()
                    if row.task_id.upper() == task_id.upper()
                ),
                None,
            )
            if record is None:
                return "not_found"
            if record.user_id != requester_user_id:
                return "forbidden"
            if record.status == "stuck":
                return "stuck"
            if record.status not in ("accepted", "slow"):
                return "not_waiting"
            record.warning_snoozed_until = (
                time.time() + config.task_progress_warn_seconds
            )
            record.updated_at = time.time()
            self._save_locked()
        await task_scheduler.extend_lease(record.window_id)
        await self._update_receipt(
            record,
            f"⏳ 继续等待 · 任务 {record.task_id}\n"
            "任务已确认启动；继续观察，不会把等待操作计作 CLI 新进展。",
            controls="slow",
        )
        self._arm_progress_watchdog(record.window_id)
        return "waiting"

    def inspect(
        self, task_id: str, *, requester_user_id: int
    ) -> tuple[str, DispatchRecord | None]:
        """Return an authorized snapshot for the task status callback."""
        record = self.record_for_task(task_id)
        if record is None:
            return "not_found", None
        if record.user_id != requester_user_id:
            return "forbidden", None
        return record.status, record

    async def check_status(
        self, task_id: str, *, requester_user_id: int
    ) -> tuple[str, str]:
        """Inspect durable dispatch state and the provider window on demand."""
        status, record = self.inspect(task_id, requester_user_id=requester_user_id)
        if status == "not_found":
            return status, "任务已经完成、归档或不存在。"
        if status == "forbidden" or record is None:
            return "forbidden", "只能检查自己的任务。"
        window = await multiplexer.find_window_by_id(record.window_id)
        if window is None:
            await self._mark_stuck(record, reason="window_missing")
            return "missing", "已确认异常：CLI 窗口不存在，任务通道已暂停。"
        idle = max(0, int(time.time() - record.last_progress_at))
        if record.status == "stuck":
            return "stuck", "已确认异常：CLI 未确认接收，请重试提交或取消任务。"
        return (
            "running",
            f"CLI 进程仍存在；最近 {max(0, idle // 60)} 分钟未检测到新输出。"
            "任务可能仍在计算，完成状态正在持续核验。",
        )

    async def retry(self, task_id: str, *, requester_user_id: int) -> str:
        with self._lock:
            record = next(
                (
                    row
                    for row in self._records.values()
                    if row.task_id.upper() == task_id.upper()
                ),
                None,
            )
            if record is None:
                return "not_found"
            if record.user_id != requester_user_id:
                return "forbidden"
            if record.status not in ("submitting", "stuck"):
                return "not_stuck"
            record.status = "submitting"
            record.retry_count += 1
            record.updated_at = time.time()
            self._save_locked()
        await task_scheduler.retry_stuck(record.window_id)
        sent = await multiplexer.send_keys(
            record.window_id, "Enter", enter=False, literal=False
        )
        DISPATCH_RETRIES.inc(
            provider=record.provider, outcome="sent" if sent else "failed"
        )
        if not sent:
            await self._mark_stuck(record, reason="manual_retry_failed")
            return "failed"
        await self._update_receipt(
            record,
            f"⌨️ 已重新提交 · 任务 {record.task_id} · 等待 {record.provider} 确认",
        )
        self._arm_ack_watchdog(record.window_id)
        return "retrying"

    async def lease_expired(self, window_id: str) -> None:
        with self._lock:
            record = self._records.get(window_id)
        if record is not None:
            await self._mark_stuck(record, reason="lease_expired")

    def _arm_ack_watchdog(self, window_id: str) -> None:
        self._cancel_watchdog(window_id)
        self._watchdogs[window_id] = self._spawn(
            self._watch_ack(window_id), name=f"dispatch-ack:{window_id}"
        )

    def _arm_progress_watchdog(self, window_id: str) -> None:
        self._cancel_watchdog(window_id)
        self._watchdogs[window_id] = self._spawn(
            self._watch_progress(window_id), name=f"dispatch-progress:{window_id}"
        )

    def _cancel_watchdog(self, window_id: str) -> None:
        task = self._watchdogs.pop(window_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _watch_ack(self, window_id: str) -> None:
        try:
            for attempt in range(config.dispatch_retry_count + 1):
                await asyncio.sleep(config.dispatch_ack_seconds)
                with self._lock:
                    record = self._records.get(window_id)
                    if record is None or record.status == "accepted":
                        return
                DISPATCH_ACK_TIMEOUTS.inc(provider=record.provider)
                if attempt >= config.dispatch_retry_count:
                    await self._mark_stuck(record, reason="ack_timeout")
                    return
                await self._update_receipt(
                    record,
                    f"⚠️ 尚未确认启动 · 任务 {record.task_id} · 正在安全补交回车",
                )
                sent = await multiplexer.send_keys(
                    window_id, "Enter", enter=False, literal=False
                )
                record.retry_count += 1
                record.updated_at = time.time()
                self._persist(record)
                DISPATCH_RETRIES.inc(
                    provider=record.provider, outcome="sent" if sent else "failed"
                )
                if not sent:
                    await self._mark_stuck(record, reason="retry_failed")
                    return
        except asyncio.CancelledError:
            return

    async def _watch_progress(self, window_id: str) -> None:
        try:
            await asyncio.sleep(config.task_progress_warn_seconds)
            with self._lock:
                record = self._records.get(window_id)
                if record is None or record.status not in ("accepted", "slow"):
                    return
                now = time.time()
                if record.warning_snoozed_until > now:
                    self._arm_progress_watchdog(window_id)
                    return
                age = time.time() - record.last_progress_at
                if age < config.task_progress_warn_seconds:
                    self._arm_progress_watchdog(window_id)
                    return
                record.status = "slow"
                record.updated_at = time.time()
                self._save_locked()
            await task_scheduler.set_phase(window_id, "slow")
            await self._update_receipt(
                record,
                f"🟠 可能停滞 · 任务 {record.task_id}\n"
                f"已确认启动，但 {max(1, int(age // 60))} 分钟未检测到新输出；"
                "这不代表已经结束，也不等于确认卡死。",
                controls="slow",
            )
            request_operations_dashboard_refresh(record.chat_id, record.thread_id)
            logger.warning(
                "Provider task has no transcript progress",
                task_id=record.task_id,
                window_id=window_id,
                provider=record.provider,
                idle_seconds=int(age),
            )
        except asyncio.CancelledError:
            return

    async def _mark_stuck(self, record: DispatchRecord, *, reason: str) -> None:
        with self._lock:
            current = self._records.get(record.window_id)
            if current is None:
                return
            current.status = "stuck"
            current.updated_at = time.time()
            record = current
            self._save_locked()
        self._cancel_watchdog(record.window_id)
        await task_scheduler.mark_stuck(record.window_id)
        await self._show_stuck(record, reason=reason)
        request_operations_dashboard_refresh(record.chat_id, record.thread_id)
        logger.warning(
            "Provider dispatch stuck; operator lane blocked",
            task_id=record.task_id,
            window_id=record.window_id,
            provider=record.provider,
            reason=reason,
            retries=record.retry_count,
        )

    async def _show_stuck(self, record: DispatchRecord, *, reason: str) -> None:
        reason_text = {
            "restart": "服务重启后状态不明确",
            "window_missing": "CLI 窗口不存在",
        }.get(reason, "CLI 未确认接收")
        await self._update_receipt(
            record,
            f"❌ 未启动 · 任务 {record.task_id} · {reason_text}\n"
            "为避免与下一问题串联，当前成员通道已暂停。",
            controls="stuck",
        )

    async def _update_receipt(
        self, record: DispatchRecord, text: str, *, controls: str | None = None
    ) -> None:
        # Lazy: task receipts import the completion callback back into here.
        from .task_receipts import update_task_receipts

        markup = None
        if controls == "stuck":
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 重试提交",
                            callback_data=f"{CB_DISPATCH_RETRY_PREFIX}{record.task_id}",
                        ),
                        InlineKeyboardButton(
                            "🛑 取消任务",
                            callback_data=f"{CB_DISPATCH_CANCEL_PREFIX}{record.task_id}",
                        ),
                    ]
                ]
            )
        elif controls == "slow":
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔎 检查状态",
                            callback_data=f"{CB_DISPATCH_CHECK_PREFIX}{record.task_id}",
                        ),
                        InlineKeyboardButton(
                            "⏳ 继续等待",
                            callback_data=f"{CB_DISPATCH_WAIT_PREFIX}{record.task_id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🛑 取消任务",
                            callback_data=f"{CB_DISPATCH_CANCEL_PREFIX}{record.task_id}",
                        )
                    ],
                ]
            )
        await update_task_receipts(record.window_id, text, reply_markup=markup)

    def _spawn(self, coroutine: Any, *, name: str | None = None) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=name)
        task.add_done_callback(task_done_callback)
        return task

    def reset_for_testing(self) -> None:
        for task in tuple(self._watchdogs.values()):
            task.cancel()
        self._watchdogs.clear()
        with self._lock:
            self._records.clear()
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._client = None


dispatch_confirmation = DispatchConfirmation(config.dispatch_state_file)


def schedule_lease_expired(window_id: str) -> None:
    try:
        task = asyncio.create_task(dispatch_confirmation.lease_expired(window_id))
    except RuntimeError:
        return
    task.add_done_callback(task_done_callback)


__all__ = [
    "CB_DISPATCH_CANCEL_PREFIX",
    "CB_DISPATCH_CHECK_PREFIX",
    "CB_DISPATCH_RETRY_PREFIX",
    "CB_DISPATCH_WAIT_PREFIX",
    "DispatchConfirmation",
    "DispatchRecord",
    "dispatch_confirmation",
    "schedule_lease_expired",
]
