"""Telegram rate limiting with quiet, request-local RetryAfter backoff."""

import asyncio
import contextlib
import random
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from telegram.error import RetryAfter
from telegram.ext import AIORateLimiter

logger = structlog.get_logger()

# PTB drops falsy per-request rate_limit_args before invoking the limiter. A
# negative truthy sentinel survives transport and means "let the caller retry".
NO_RETRY_RATE_LIMIT_ARGS = -1

_RETRY_BACKOFF_BASE_SECONDS = 1.0
_MAX_RETRY_BACKOFF_SECONDS = 8.0
_RETRY_JITTER_MAX_SECONDS = 1.0


def retry_after_seconds(exc: RetryAfter) -> float:
    """Normalize PTB's int/timedelta RetryAfter values."""
    value = exc.retry_after
    return float(value if isinstance(value, int) else value.total_seconds())


class CCGramAIORateLimiter(AIORateLimiter):
    """Retry limited calls independently without a global reactive pause.

    PTB's proactive overall/group budgets remain active. A Telegram
    ``RetryAfter`` only sleeps the affected request, so one topic rename cannot
    freeze replies in unrelated chats. Expected flood control is logged as a
    concise warning rather than an exception traceback.
    """

    async def process_request(
        self,
        callback: Callable[..., Coroutine[Any, Any, Any]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: int | None,
    ) -> Any:
        chat_id = data.get("chat_id")
        if chat_id is not None:
            with contextlib.suppress(TypeError, ValueError):
                chat_id = int(chat_id)
        group: int | str | bool = False
        if (isinstance(chat_id, int) and chat_id < 0) or isinstance(chat_id, str):
            group = chat_id

        async def run_request() -> Any:
            return await self._run_request(
                chat=chat_id is not None,
                group=group,
                allow_paid_broadcast=data.get("allow_paid_broadcast", False),
                callback=callback,
                args=args,
                kwargs=kwargs,
            )

        if rate_limit_args is not None and rate_limit_args <= 0:
            return await run_request()

        max_retries = self._max_retries if rate_limit_args is None else rate_limit_args
        for retry in range(max_retries + 1):
            try:
                return await run_request()
            except RetryAfter as exc:
                retry_after = retry_after_seconds(exc)
                if retry == max_retries:
                    logger.warning(
                        "Telegram rate limit persisted; returning request to caller",
                        endpoint=endpoint,
                        attempts=retry + 1,
                        retry_after_seconds=retry_after,
                    )
                    raise

                backoff = min(
                    _MAX_RETRY_BACKOFF_SECONDS,
                    _RETRY_BACKOFF_BASE_SECONDS * (2**retry),
                )
                jitter = random.uniform(0, _RETRY_JITTER_MAX_SECONDS)
                retry_in = retry_after + backoff + jitter
                logger.warning(
                    "Telegram rate limited; retrying later",
                    endpoint=endpoint,
                    retry=retry + 1,
                    max_retries=max_retries,
                    retry_after_seconds=retry_after,
                    backoff_seconds=backoff,
                    jitter_seconds=jitter,
                    retry_in_seconds=retry_in,
                )
                await asyncio.sleep(retry_in)

        raise AssertionError("unreachable")
