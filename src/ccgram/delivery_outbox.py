"""Crash-safe outbound assistant-message journal.

Entries are written before they enter an in-memory queue and removed only
after Telegram returns a Message.  A bounded delivered-id set makes transcript
replay idempotent when the process dies between Telegram's acknowledgement and
the monitor cursor commit.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from .config import config
from .metrics import OUTBOX_ITEMS
from .utils import atomic_write_json

logger = structlog.get_logger()
_DELIVERED_LIMIT = 2000

if TYPE_CHECKING:
    from .handlers.messaging_pipeline.message_task import ContentTask


class DeliveryOutbox:
    def __init__(self) -> None:
        self._path = config.outbox_file
        self._lock = asyncio.Lock()
        self._loaded = False
        self._pending: dict[str, dict[str, Any]] = {}
        self._delivered: dict[str, float] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text())
            self._pending = dict(raw.get("pending", {}))
            self._delivered = {
                str(key): float(value)
                for key, value in dict(raw.get("delivered", {})).items()
            }
        except FileNotFoundError:
            pass
        except OSError, ValueError, TypeError:
            logger.exception("Could not read delivery outbox; starting empty")
        finally:
            self._sync_metrics()

    def _sync_metrics(self) -> None:
        """Reconcile gauges with durable state, including an empty restart."""
        OUTBOX_ITEMS.set(len(self._pending), state="pending")
        OUTBOX_ITEMS.set(
            sum(bool(row.get("last_error")) for row in self._pending.values()),
            state="retrying",
        )

    async def _save(self) -> None:
        delivered = sorted(self._delivered.items(), key=lambda item: item[1])
        self._delivered = dict(delivered[-_DELIVERED_LIMIT:])
        data = {"version": 1, "pending": self._pending, "delivered": self._delivered}
        await asyncio.to_thread(atomic_write_json, self._path, data)
        self._sync_metrics()

    async def add(self, task: ContentTask, user_id: int) -> bool:
        """Persist *task* before enqueue; False means it is already known."""
        if not task.delivery_id:
            return True
        async with self._lock:
            self._load()
            if task.delivery_id in self._pending or task.delivery_id in self._delivered:
                return False
            record = asdict(task)
            record["parts"] = list(task.parts)
            record["user_id"] = user_id
            record["attempts"] = 0
            record["last_error"] = ""
            record["created_at"] = time.time()
            self._pending[task.delivery_id] = record
            await self._save()
            return True

    async def advance(self, delivery_id: str, next_part: int) -> None:
        async with self._lock:
            self._load()
            if record := self._pending.get(delivery_id):
                record["next_part"] = next_part
                await self._save()

    async def failed(self, delivery_id: str, error: str) -> int:
        async with self._lock:
            self._load()
            record = self._pending.get(delivery_id)
            if not record:
                return 0
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["last_error"] = error[:500]
            await self._save()
            return int(record["attempts"])

    async def delivered(self, delivery_id: str) -> None:
        async with self._lock:
            self._load()
            self._pending.pop(delivery_id, None)
            self._delivered[delivery_id] = time.time()
            await self._save()

    def has_pending_session(self, session_id: str) -> bool:
        self._load()
        return any(r.get("session_id") == session_id for r in self._pending.values())

    def snapshot(self) -> tuple[int, int]:
        self._load()
        failed = sum(bool(r.get("last_error")) for r in self._pending.values())
        return len(self._pending), failed

    def attempts(self, delivery_id: str) -> int:
        self._load()
        return int(self._pending.get(delivery_id, {}).get("attempts", 0))

    def pending_tasks(self) -> list[tuple[int, ContentTask]]:
        # Lazy: avoid a package-init cycle (message_queue imports this singleton).
        from .handlers.messaging_pipeline.message_task import ContentTask

        self._load()
        restored: list[tuple[int, ContentTask]] = []
        for record in self._pending.values():
            fields = {
                key: value
                for key, value in record.items()
                if key in ContentTask.__dataclass_fields__
            }
            fields["parts"] = tuple(fields.get("parts", ()))
            restored.append((int(record["user_id"]), ContentTask(**fields)))
        return restored

    def reset_for_testing(self) -> None:
        self._loaded = False
        self._pending.clear()
        self._delivered.clear()
        self._path = config.outbox_file
        self._sync_metrics()


delivery_outbox = DeliveryOutbox()
