"""Durable, non-sensitive audit trail for task cancellation actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import threading
import time

import structlog

from .config import config
from .metrics import TASK_CANCELLATIONS

logger = structlog.get_logger()
_MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class TaskAuditEvent:
    timestamp: float
    action: str
    task_id: str
    requester_user_id: int
    owner_user_id: int
    chat_id: int
    thread_id: int
    window_id: str
    detail: str = ""


def record_task_audit(
    action: str,
    *,
    task_id: str,
    requester_user_id: int,
    owner_user_id: int,
    chat_id: int,
    thread_id: int,
    window_id: str = "",
    detail: str = "",
) -> None:
    """Append one mode-0600 JSONL event and emit its metric/log record."""
    event = TaskAuditEvent(
        timestamp=time.time(),
        action=action,
        task_id=task_id,
        requester_user_id=requester_user_id,
        owner_user_id=owner_user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        detail=detail,
    )
    path = config.task_audit_file
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size >= _MAX_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            path.replace(rotated)
            os.chmod(rotated, 0o600)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    TASK_CANCELLATIONS.inc(outcome=action)
    logger.info("task_cancellation_audit", **asdict(event))


def cancellation_summary(*, hours: int = 24) -> dict[str, int]:
    """Count recent cancellation outcomes without exposing actor IDs."""
    cutoff = time.time() - max(1, hours) * 3600
    result: dict[str, int] = {}
    path: Path = config.task_audit_file
    for candidate in (path.with_suffix(path.suffix + ".1"), path):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                if float(row.get("timestamp", 0)) < cutoff:
                    continue
                action = str(row.get("action", "unknown"))
                result[action] = result.get(action, 0) + 1
            except ValueError, TypeError, json.JSONDecodeError:
                continue
    return result


__all__ = ["cancellation_summary", "record_task_audit"]
