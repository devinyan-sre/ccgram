"""End-to-end correlation IDs for the inbound message path.

structlog is already configured with ``merge_contextvars`` first in the
processor chain, and several loops bind ``session_id`` / ``window_id``. What
was missing is a per-message id that survives the hop from the routing
coroutine into the queue worker — a *different* task — so a single message can
be followed end to end: routing → enqueue → worker → send.

A ``ContentTask`` / ``StatusTask`` carries its ``cid`` as data; the worker
rebinds it before dispatch (contextvars do not cross the task boundary on their
own). Ids are short and derived from a monotonic counter, not wall-clock or
randomness — both are unavailable/discouraged in some execution contexts here,
and a counter is deterministic under test.
"""

from __future__ import annotations

import itertools

import structlog

_counter = itertools.count(1)

_CID_KEY = "cid"


def new_cid() -> str:
    """Return a fresh short correlation id (process-local, monotonic)."""
    return f"m{next(_counter):x}"


def bind_cid(cid: str | None) -> None:
    """Bind ``cid`` into the structlog contextvars for the current task.

    A None cid is a no-op, so the worker can call this unconditionally for
    tasks that predate correlation or were enqueued outside a bound context.
    """
    if cid is not None:
        structlog.contextvars.bind_contextvars(**{_CID_KEY: cid})


def current_cid() -> str | None:
    """Read the cid bound in the current task's context, if any.

    Lets the enqueue helpers stamp the routing coroutine's cid onto a task as
    data — so the worker, running in a *different* task where these contextvars
    do not apply, can rebind it — without every caller passing it explicitly.
    """
    value = structlog.contextvars.get_contextvars().get(_CID_KEY)
    return value if isinstance(value, str) else None


def reset_for_testing() -> None:
    """Restart the counter (test hook for stable ids)."""
    global _counter
    _counter = itertools.count(1)
