"""Durable inbound task journal and Telegram message idempotency guard.

The journal deliberately distinguishes ``queued`` from ``dispatching``. Queued
messages are safe to resume after a restart. A process that died while a send
was in flight leaves an ambiguous ``dispatching`` record; ccgram never replays
that record because doing so could execute a provider task twice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Literal
from typing import cast

import structlog

from .config import config
from .metrics import INBOUND_DUPLICATES

logger = structlog.get_logger()

InboundState = Literal[
    "queued", "dispatching", "forwarded", "done", "failed", "interrupted"
]


@dataclass(slots=True)
class InboundItem:
    key: str
    chat_id: int
    thread_id: int
    user_id: int
    message_id: int
    window_id: str
    text: str
    state: InboundState
    created_at: float
    updated_at: float

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "InboundItem":
        return cls(
            key=str(data["key"]),
            chat_id=int(str(data["chat_id"])),
            thread_id=int(str(data["thread_id"])),
            user_id=int(str(data["user_id"])),
            message_id=int(str(data["message_id"])),
            window_id=str(data["window_id"]),
            text=str(data.get("text", "")),
            state=cast(InboundState, str(data.get("state", "failed"))),
            created_at=float(str(data.get("created_at", 0.0))),
            updated_at=float(str(data.get("updated_at", 0.0))),
        )


class InboundStore:
    """Small atomic JSON journal; all public mutations are thread-safe."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._items: dict[str, InboundItem] = {}
        self._seen: dict[str, float] = {}
        self._load()

    @staticmethod
    def make_key(chat_id: int, thread_id: int, message_id: int) -> str:
        return f"{chat_id}:{thread_id}:{message_id}"

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("items", []) if isinstance(raw, dict) else []
            seen = raw.get("seen", {}) if isinstance(raw, dict) else {}
            if isinstance(seen, dict):
                self._seen = {
                    str(key): float(value)
                    for key, value in seen.items()
                    if isinstance(value, int | float)
                }
            for row in rows:
                if isinstance(row, dict):
                    item = InboundItem.from_dict(row)
                    self._items[item.key] = item
            self._prune_locked(time.time())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not load inbound journal", error=str(exc))

    def _prune_locked(self, now: float) -> None:
        cutoff = now - config.inbound_dedupe_hours * 3600
        self._items = {
            key: item
            for key, item in self._items.items()
            if item.updated_at >= cutoff or item.state not in ("done", "failed")
        }
        self._seen = {
            key: timestamp
            for key, timestamp in self._seen.items()
            if timestamp >= cutoff or key in self._items
        }

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": [asdict(item) for item in self._items.values()],
            "seen": self._seen,
        }
        fd, temp_name = tempfile.mkstemp(prefix=".inbound-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)

    def stage(
        self,
        *,
        chat_id: int,
        thread_id: int,
        user_id: int,
        message_id: int,
        window_id: str,
        text: str,
    ) -> bool:
        """Persist a queued message; return False when it is a duplicate."""
        key = self.make_key(chat_id, thread_id, message_id)
        with self._lock:
            self._prune_locked(time.time())
            if key in self._items:
                INBOUND_DUPLICATES.inc()
                return False
            now = time.time()
            self._seen[key] = now
            self._items[key] = InboundItem(
                key=key,
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                message_id=message_id,
                window_id=window_id,
                text=text,
                state="queued",
                created_at=now,
                updated_at=now,
            )
            self._save_locked()
        return True

    def claim_message(self, *, chat_id: int, thread_id: int, message_id: int) -> bool:
        """Durably reserve a Telegram message ID before delayed coalescing."""
        key = self.make_key(chat_id, thread_id, message_id)
        with self._lock:
            self._prune_locked(time.time())
            if key in self._seen:
                INBOUND_DUPLICATES.inc()
                return False
            self._seen[key] = time.time()
            self._save_locked()
            return True

    def set_state(self, key: str, state: InboundState) -> None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return
            item.state = state
            item.updated_at = time.time()
            self._save_locked()

    def mark_window_done(self, window_id: str, *, failed: bool = False) -> None:
        with self._lock:
            changed = False
            for item in self._items.values():
                if item.window_id == window_id and item.state in (
                    "queued",
                    "dispatching",
                    "forwarded",
                ):
                    item.state = "failed" if failed else "done"
                    item.updated_at = time.time()
                    changed = True
            if changed:
                self._save_locked()

    def recoverable(self) -> list[InboundItem]:
        with self._lock:
            return [
                InboundItem(**asdict(item))
                for item in self._items.values()
                if item.state == "queued"
            ]

    def interrupt_ambiguous(self) -> list[InboundItem]:
        """Fail closed on sends that may have reached a CLI before a crash."""
        with self._lock:
            affected: list[InboundItem] = []
            for item in self._items.values():
                if item.state != "dispatching":
                    continue
                item.state = "interrupted"
                item.updated_at = time.time()
                affected.append(InboundItem(**asdict(item)))
            if affected:
                self._save_locked()
            return affected

    def active_for_window(self, window_id: str) -> InboundItem | None:
        with self._lock:
            candidates = [
                item
                for item in self._items.values()
                if item.window_id == window_id
                and item.state in ("dispatching", "forwarded")
            ]
            if not candidates:
                return None
            return InboundItem(
                **asdict(min(candidates, key=lambda item: item.created_at))
            )

    def clear_for_testing(self) -> None:
        with self._lock:
            self._items.clear()
            self._seen.clear()


inbound_store = InboundStore(config.inbound_file)

__all__ = ["InboundItem", "InboundStore", "inbound_store"]
