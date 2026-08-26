"""Bounded Telegram update concurrency with per-operator ordering.

Different allow-listed operators can work in parallel, including inside the
same forum topic.  Updates from one operator are serialized so PTB ``user_data``
state machines (directory browser, callbacks, recovery) cannot race each other.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable
from typing import Any

from telegram import Update
from telegram.ext import BaseUpdateProcessor


class OperatorUpdateProcessor(BaseUpdateProcessor):
    """Process operators concurrently while preserving per-user FIFO order."""

    def __init__(self, max_concurrent_updates: int) -> None:
        super().__init__(max_concurrent_updates)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def _operator_key(update: object) -> int:
        if isinstance(update, Update) and update.effective_user is not None:
            return update.effective_user.id
        # Non-Update objects are rare (and generally synthetic); serialize
        # them behind one fail-safe lane rather than guessing identity.
        return 0

    async def do_process_update(
        self, update: object, coroutine: Awaitable[Any]
    ) -> None:
        async with self._locks[self._operator_key(update)]:
            await coroutine

    async def initialize(self) -> None:
        """No external resources to initialize."""

    async def shutdown(self) -> None:
        """Release idle lock references on application shutdown."""
        self._locks.clear()


__all__ = ["OperatorUpdateProcessor"]
