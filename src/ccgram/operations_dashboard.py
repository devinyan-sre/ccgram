"""Persistent Telegram operations dashboards for groups and forum topics.

The dashboard is provider-neutral: it renders the shared task scheduler and
thread router rather than reading Claude/Codex/Gemini-specific output.  Each
target owns one message which is edited in place and, when permitted, pinned.
Message IDs and operator display labels survive process restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, TelegramError

from .config import config
from .i18n import t
from .task_scheduler import TaskView, task_scheduler
from .telegram_client import TelegramClient
from .thread_router import thread_router
from .user_time import now_display
from .utils import task_done_callback

logger = structlog.get_logger()

CB_DASHBOARD_REFRESH = "od:refresh"
_GENERAL_THREAD_ID = 1
_PIN_RETRY_SECONDS = 3600.0
_TARGET_RETRY_SECONDS = 3600.0
_TRANSIENT_RETRY_SECONDS = 60.0
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_IDLE_STAMP_MINUTES = 5


@dataclass(frozen=True, slots=True)
class DashboardTarget:
    chat_id: int
    thread_id: int
    global_view: bool

    @property
    def key(self) -> str:
        scope = "general" if self.global_view else "topic"
        return f"{scope}:{self.chat_id}:{self.thread_id}"


@dataclass(slots=True)
class _CompletedTask:
    view: TaskView
    ended_at: float


class OperationsDashboard:
    """Own and refresh one editable overview message per configured target."""

    def __init__(self, client: TelegramClient, state_path: Path) -> None:
        self._client = client
        self._state_path = state_path
        self._message_ids: dict[str, int] = {}
        self._operator_labels: dict[int, str] = {}
        self._last_text: dict[str, str] = {}
        self._pin_retry_at: dict[str, float] = {}
        self._target_retry_at: dict[str, float] = {}
        self._previous_tasks: dict[tuple[int, int, str], TaskView] = {}
        self._completed: dict[tuple[int, int, str], _CompletedTask] = {}
        self._refresh_event = asyncio.Event()
        self._render_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._message_ids = {
                str(key): int(value)
                for key, value in raw.get("messages", {}).items()
                if int(value) > 0
            }
            self._operator_labels = {
                int(key): str(value)[:64]
                for key, value in raw.get("operators", {}).items()
                if str(value).strip()
            }
        except OSError, ValueError, TypeError:
            logger.warning("Could not load operations dashboard state", exc_info=True)

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".dashboard-", dir=self._state_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "messages": self._message_ids,
                        "operators": {
                            str(key): value
                            for key, value in self._operator_labels.items()
                        },
                    },
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self._state_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)

    def observe_user(self, user: Any) -> None:
        """Remember a safe human label from an authorized Telegram update."""
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int):
            return
        username = str(getattr(user, "username", "") or "").strip()
        full_name = str(getattr(user, "full_name", "") or "").strip()
        label = f"@{username}" if username else full_name
        label = " ".join(label.replace("\n", " ").split())[:48]
        if label and self._operator_labels.get(user_id) != label:
            self._operator_labels[user_id] = label
            self._save_state()
            self.request_refresh()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="operations-dashboard")
        self._task.add_done_callback(task_done_callback)
        logger.info(
            "Operations dashboard started",
            scope=config.dashboard_scope,
            refresh_seconds=config.dashboard_refresh_seconds,
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("Operations dashboard stopped")

    def request_refresh(self) -> None:
        self._refresh_event.set()

    async def refresh_target(self, chat_id: int, thread_id: int) -> bool:
        """Force-refresh the dashboard containing the callback message."""
        targets = self._targets()
        target = next(
            (
                candidate
                for candidate in targets
                if candidate.chat_id == chat_id and candidate.thread_id == thread_id
            ),
            None,
        )
        if target is None:
            return False
        async with self._render_lock:
            self._capture_completions()
            await self._upsert(target, force=True)
        return True

    async def _run(self) -> None:
        while True:
            try:
                async with self._render_lock:
                    self._capture_completions()
                    for target in self._targets():
                        await self._upsert(target)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._refresh_event.wait(),
                        timeout=config.dashboard_refresh_seconds,
                    )
                self._refresh_event.clear()
            except asyncio.CancelledError:
                raise
            except RetryAfter as exc:
                retry_after = exc.retry_after
                retry_seconds = (
                    retry_after.total_seconds()
                    if isinstance(retry_after, timedelta)
                    else float(retry_after)
                )
                delay = min(30.0, max(1.0, retry_seconds))
                logger.warning("Dashboard rate limited", retry_after=delay)
                await asyncio.sleep(delay)
            except Exception:  # optional observer must self-heal
                logger.exception("Operations dashboard refresh failed")
                await asyncio.sleep(config.dashboard_refresh_seconds)

    def _targets(self) -> list[DashboardTarget]:
        chats: set[int] = set()
        if config.group_id is not None and config.group_id < 0:
            chats.add(config.group_id)
        chats.update(
            chat_id
            for chat_id in thread_router.group_chat_ids.values()
            if isinstance(chat_id, int) and chat_id < 0
        )

        targets: list[DashboardTarget] = []
        if config.dashboard_scope in ("general", "both"):
            targets.extend(
                DashboardTarget(chat_id, _GENERAL_THREAD_ID, True)
                for chat_id in sorted(chats)
            )

        if config.dashboard_scope in ("topic", "both"):
            physical_topics: set[tuple[int, int]] = set()
            if config.group_id is not None and config.group_id < 0:
                physical_topics.update(
                    (config.group_id, thread_id)
                    for _user_id, thread_id, _window_id in (
                        thread_router.iter_thread_bindings()
                    )
                    if isinstance(thread_id, int) and thread_id > _GENERAL_THREAD_ID
                )
            for key, chat_id in thread_router.group_chat_ids.items():
                try:
                    _user_id, raw_thread = key.split(":", 1)
                    thread_id = int(raw_thread)
                except ValueError, TypeError:
                    continue
                if (
                    isinstance(chat_id, int)
                    and chat_id < 0
                    and thread_id > _GENERAL_THREAD_ID
                ):
                    physical_topics.add((chat_id, thread_id))
            targets.extend(
                DashboardTarget(chat_id, thread_id, False)
                for chat_id, thread_id in sorted(physical_topics)
            )
        return targets

    def _capture_completions(self) -> None:
        now = time.time()
        current_views = task_scheduler.views()
        current = {
            (view.chat_id, view.thread_id, view.task_id): view for view in current_views
        }
        for key, old_view in self._previous_tasks.items():
            if key not in current:
                self._completed[key] = _CompletedTask(old_view, now)
        for key in current:
            self._completed.pop(key, None)
        ttl = config.dashboard_completed_ttl_seconds
        self._completed = {
            key: row
            for key, row in self._completed.items()
            if ttl > 0 and now - row.ended_at < ttl
        }
        self._previous_tasks = current

    async def _upsert(self, target: DashboardTarget, *, force: bool = False) -> None:
        if not force and time.monotonic() < self._target_retry_at.get(target.key, 0.0):
            return
        text = self._render(target, precise_time=force)
        if not force and self._last_text.get(target.key) == text:
            return
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("🔄 Refresh"), callback_data=CB_DASHBOARD_REFRESH
                    )
                ]
            ]
        )
        message_id = self._message_ids.get(target.key)
        if message_id is None:
            await self._create(target, text, markup)
            return
        try:
            await self._client.edit_message_text(
                text=text,
                chat_id=target.chat_id,
                message_id=message_id,
                reply_markup=markup,
            )
            self._last_text[target.key] = text
            await self._try_pin(target, message_id)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                self._last_text[target.key] = text
                return
            # Deleted, inaccessible, or too-old messages are replaced once;
            # this also recovers cleanly after an operator deletes a dashboard.
            logger.warning(
                "Dashboard message could not be edited; recreating",
                target=target.key,
                error=str(exc),
            )
            self._message_ids.pop(target.key, None)
            self._last_text.pop(target.key, None)
            self._save_state()
            await self._create(target, text, markup)
        except TelegramError as exc:
            logger.warning(
                "Dashboard message edit failed", target=target.key, error=str(exc)
            )

    async def _create(
        self,
        target: DashboardTarget,
        text: str,
        markup: InlineKeyboardMarkup,
    ) -> None:
        # Telegram represents General inconsistently across updates. Sending
        # without message_thread_id is the portable Bot API form; an explicit
        # ``1`` is rejected as "Message thread not found" in some forum groups.
        thread_kwargs = (
            {} if target.global_view else {"message_thread_id": target.thread_id}
        )
        try:
            sent = await self._client.send_message(
                chat_id=target.chat_id,
                text=text,
                reply_markup=markup,
                disable_notification=True,
                **thread_kwargs,
            )
        except RetryAfter:
            raise
        except BadRequest as exc:
            retry = (
                _TARGET_RETRY_SECONDS
                if "message thread not found" in str(exc).lower()
                else _TRANSIENT_RETRY_SECONDS
            )
            self._target_retry_at[target.key] = time.monotonic() + retry
            logger.warning(
                "Dashboard target unavailable; backing off",
                target=target.key,
                retry_seconds=int(retry),
                error=str(exc),
            )
            return
        except TelegramError as exc:
            self._target_retry_at[target.key] = (
                time.monotonic() + _TRANSIENT_RETRY_SECONDS
            )
            logger.warning(
                "Dashboard message creation failed", target=target.key, error=str(exc)
            )
            return
        message_id = getattr(sent, "message_id", None)
        if not isinstance(message_id, int):
            logger.warning("Dashboard send returned no message id", target=target.key)
            return
        self._message_ids[target.key] = message_id
        self._last_text[target.key] = text
        self._save_state()
        await self._try_pin(target, message_id)

    async def _try_pin(self, target: DashboardTarget, message_id: int) -> None:
        if not config.dashboard_pin:
            return
        now = time.monotonic()
        if now < self._pin_retry_at.get(target.key, 0.0):
            return
        try:
            await self._client.pin_chat_message(
                chat_id=target.chat_id,
                message_id=message_id,
                disable_notification=True,
            )
        except TelegramError as exc:
            # Posting/editing remains useful without the admin pin permission.
            self._pin_retry_at[target.key] = now + _PIN_RETRY_SECONDS
            logger.warning(
                "Dashboard pin unavailable; continuing unpinned",
                target=target.key,
                error=str(exc),
            )
        finally:
            # Reassert occasionally so a manual/unexpected unpin self-heals,
            # while avoiding a pin API call on every dashboard edit.
            self._pin_retry_at[target.key] = now + _PIN_RETRY_SECONDS

    def _render(self, target: DashboardTarget, *, precise_time: bool) -> str:
        views = task_scheduler.views(chat_id=target.chat_id)
        if not target.global_view:
            views = [view for view in views if view.thread_id == target.thread_id]
        completed = [
            row
            for row in self._completed.values()
            if row.view.chat_id == target.chat_id
            and (target.global_view or row.view.thread_id == target.thread_id)
        ]

        active = sum(view.state == "active" for view in views)
        cancelling = sum(view.state == "cancelling" for view in views)
        queued = sum(view.state == "queued" for view in views)
        if target.global_view:
            title = t("🛰 CCGram operations overview")
            cap = config.max_parallel_global
        else:
            title = t("🛰 Topic operations overview") + f" · {self._topic_name(target)}"
            cap = config.max_parallel_per_topic
        lines = [
            title,
            t(
                "Concurrency {used}/{limit} · queued {queued} · cancelling {cancelling}"
            ).format(
                used=active + cancelling,
                limit=cap,
                queued=queued,
                cancelling=cancelling,
            ),
            "",
        ]

        rows: list[str] = []
        ordered = sorted(
            views,
            key=lambda view: (
                {"active": 0, "cancelling": 1, "queued": 2}.get(view.state, 3),
                view.queue_position,
                view.task_id,
            ),
        )
        for view in ordered:
            rows.append(self._render_task(view, include_topic=target.global_view))
        for row in sorted(completed, key=lambda item: item.ended_at, reverse=True):
            rows.append(self._render_completed(row, include_topic=target.global_view))

        visible = rows[: config.dashboard_max_items]
        if visible:
            lines.extend(visible)
            hidden = len(rows) - len(visible)
            if hidden:
                lines.append(t("… and {count} more").format(count=hidden))
        else:
            lines.append(t("⚪ No active or queued tasks"))

        now = now_display()
        if not precise_time:
            # Idle topic dashboards must not consume the group's Telegram API
            # budget once per minute. State changes still render immediately;
            # this timestamp only advances in coarse five-minute buckets.
            now = now.replace(
                minute=now.minute - (now.minute % _IDLE_STAMP_MINUTES),
                second=0,
                microsecond=0,
            )
        stamp_format = "%H:%M:%S" if precise_time else "%H:%M"
        timezone_label = (
            t("Beijing Time")
            if config.timezone_name == "Asia/Shanghai"
            else config.timezone_name
        )
        lines.extend(
            [
                "",
                t(
                    "Updated {time} · one operator is serial, operators are parallel"
                ).format(time=f"{now.strftime(stamp_format)} {timezone_label}"),
            ]
        )
        return "\n".join(lines)[:4096]

    def _render_task(self, view: TaskView, *, include_topic: bool) -> str:
        if view.state == "queued":
            state = t("🟡 queued #{position}").format(position=view.queue_position)
            suffix = (
                t(" · ETA ≤{seconds}s").format(seconds=view.estimated_wait_seconds)
                if view.estimated_wait_seconds is not None
                else ""
            )
        elif view.state == "cancelling":
            state = t("🟠 cancelling")
            suffix = ""
        else:
            state = t("🟢 processing")
            suffix = ""
        topic = f" · {self._topic_name_for_view(view)}" if include_topic else ""
        supplements = (
            t(" · +{count} supplements").format(count=view.supplements)
            if view.supplements
            else ""
        )
        return (
            f"{state} · {view.task_id} · {self._operator(view.user_id)}"
            f"{topic} · {self._duration(view.age_seconds)}{supplements}{suffix}"
        )

    def _render_completed(self, row: _CompletedTask, *, include_topic: bool) -> str:
        topic = f" · {self._topic_name_for_view(row.view)}" if include_topic else ""
        outcome = (
            t("cancelled") if row.view.state in ("queued", "cancelling") else t("ended")
        )
        return (
            f"✅ {row.view.task_id} · {self._operator(row.view.user_id)}{topic}"
            f" · {outcome} {self._duration(time.time() - row.ended_at)} ago"
        )

    def _operator(self, user_id: int) -> str:
        digest = hashlib.sha256(str(user_id).encode()).hexdigest()[:4].upper()
        if config.dashboard_privacy == "strict":
            return t("member-{code}").format(code=digest)
        return self._operator_labels.get(
            user_id, t("member-{code}").format(code=digest)
        )

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < _SECONDS_PER_MINUTE:
            return t("{seconds}s").format(seconds=(seconds // 30) * 30)
        if seconds < _SECONDS_PER_HOUR:
            return t("{minutes}m").format(minutes=seconds // _SECONDS_PER_MINUTE)
        return t("{hours}h{minutes}m").format(
            hours=seconds // _SECONDS_PER_HOUR,
            minutes=(seconds % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE,
        )

    @staticmethod
    def _topic_name(target: DashboardTarget) -> str:
        window_id = thread_router.get_workspace_window_for_chat_thread(
            target.chat_id, target.thread_id
        )
        return (
            thread_router.get_display_name(window_id)
            if window_id is not None
            else t("topic {thread_id}").format(thread_id=target.thread_id)
        )

    @staticmethod
    def _topic_name_for_view(view: TaskView) -> str:
        window_id = thread_router.get_workspace_window_for_chat_thread(
            view.chat_id, view.thread_id
        )
        return thread_router.get_display_name(window_id or view.window_id)


_active_dashboard: OperationsDashboard | None = None


def start_operations_dashboard(client: TelegramClient) -> OperationsDashboard | None:
    global _active_dashboard
    if not config.dashboard_enabled:
        return None
    if _active_dashboard is None:
        _active_dashboard = OperationsDashboard(client, config.dashboard_state_file)
    _active_dashboard.start()
    return _active_dashboard


async def stop_operations_dashboard() -> None:
    global _active_dashboard
    dashboard = _active_dashboard
    _active_dashboard = None
    if dashboard is not None:
        await dashboard.stop()


def observe_dashboard_user(user: Any) -> None:
    if _active_dashboard is not None:
        _active_dashboard.observe_user(user)


async def refresh_operations_dashboard(chat_id: int, thread_id: int) -> bool:
    if _active_dashboard is None:
        return False
    return await _active_dashboard.refresh_target(chat_id, thread_id)


def dashboard_owns_general_pin() -> bool:
    return config.dashboard_enabled and config.dashboard_scope in ("general", "both")


def reset_for_testing() -> None:
    global _active_dashboard
    _active_dashboard = None


__all__ = [
    "CB_DASHBOARD_REFRESH",
    "DashboardTarget",
    "OperationsDashboard",
    "dashboard_owns_general_pin",
    "observe_dashboard_user",
    "refresh_operations_dashboard",
    "start_operations_dashboard",
    "stop_operations_dashboard",
]
