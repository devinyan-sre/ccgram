"""Short-window aggregation for rapid supplements from the same operator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from telegram import Update

logger = structlog.get_logger()

FlushCallback = Callable[[Update, Any, str], Awaitable[None]]


@dataclass(slots=True)
class _Batch:
    update: Update
    context: Any
    callback: FlushCallback
    texts: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class MessageCoalescer:
    def __init__(self) -> None:
        self._batches: dict[tuple[int, int, int], _Batch] = {}

    async def submit(
        self,
        *,
        key: tuple[int, int, int],
        update: Update,
        context: Any,
        text: str,
        delay_ms: int,
        callback: FlushCallback,
    ) -> None:
        batch = self._batches.get(key)
        if batch is None:
            batch = _Batch(update=update, context=context, callback=callback)
            self._batches[key] = batch
        batch.texts.append(text)
        if batch.task is not None:
            batch.task.cancel()
        batch.task = asyncio.create_task(
            self._flush_after(key, delay_ms / 1000),
            name=f"message-coalesce:{key[0]}:{key[1]}:{key[2]}",
        )

    async def _flush_after(self, key: tuple[int, int, int], delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            batch = self._batches.pop(key, None)
            if batch is None:
                return
            combined = "\n\n".join(batch.texts)
            if len(batch.texts) > 1 and batch.update.effective_message is not None:
                await batch.update.effective_message.reply_text(
                    f"🧩 已将连续的 {len(batch.texts)} 条消息合并为一个任务。"
                )
            await batch.callback(batch.update, batch.context, combined)
        except asyncio.CancelledError:
            return
        except BaseException:
            logger.exception("Coalesced message dispatch failed", key=key)

    async def flush_all(self) -> None:
        """Flush pending batches during a graceful shutdown."""
        keys = tuple(self._batches)
        for key in keys:
            batch = self._batches.pop(key, None)
            if batch is None:
                continue
            if batch.task is not None:
                batch.task.cancel()
            await batch.callback(batch.update, batch.context, "\n\n".join(batch.texts))

    def reset_for_testing(self) -> None:
        for batch in self._batches.values():
            if batch.task is not None:
                batch.task.cancel()
        self._batches.clear()


message_coalescer = MessageCoalescer()

__all__ = ["MessageCoalescer", "message_coalescer"]
