"""Request-local Telegram RetryAfter handling."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, call, patch

import pytest
from telegram.error import RetryAfter
from telegram.ext import ExtBot

from ccgram.telegram_rate_limiter import (
    CCGramAIORateLimiter,
    NO_RETRY_RATE_LIMIT_ARGS,
)


@pytest.mark.parametrize("rate_limit_args", [0, -2, NO_RETRY_RATE_LIMIT_ARGS])
async def test_no_retry_override_propagates_first_retry_after(
    rate_limit_args: int,
) -> None:
    limiter = CCGramAIORateLimiter(max_retries=5)
    callback = AsyncMock(side_effect=RetryAfter(timedelta(seconds=3)))

    with pytest.raises(RetryAfter):
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="editForumTopic",
            data={"chat_id": 42},
            rate_limit_args=rate_limit_args,
        )

    callback.assert_awaited_once()


def test_no_retry_sentinel_survives_extbot_transport() -> None:
    api_kwargs = ExtBot._merge_api_rl_kwargs(  # pyright: ignore[reportPrivateUsage]
        None, NO_RETRY_RATE_LIMIT_ARGS
    )

    assert api_kwargs is not None
    assert NO_RETRY_RATE_LIMIT_ARGS in api_kwargs.values()


async def test_default_policy_uses_bounded_exponential_backoff() -> None:
    limiter = CCGramAIORateLimiter(max_retries=3)
    callback = AsyncMock(
        side_effect=[
            RetryAfter(timedelta(seconds=3)),
            RetryAfter(timedelta(seconds=3)),
            RetryAfter(timedelta(seconds=3)),
            True,
        ]
    )

    with (
        patch("ccgram.telegram_rate_limiter.asyncio.sleep", new=AsyncMock()) as sleep,
        patch("ccgram.telegram_rate_limiter.random.uniform", return_value=0.25),
    ):
        result = await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="sendMessage",
            data={"chat_id": 42},
            rate_limit_args=None,
        )

    assert result is True
    assert sleep.await_args_list == [call(4.25), call(5.25), call(7.25)]


async def test_retry_after_does_not_pause_unrelated_chat() -> None:
    limiter = CCGramAIORateLimiter(max_retries=1)
    retry_sleep_started = asyncio.Event()
    release_retry = asyncio.Event()
    retrying = AsyncMock(side_effect=[RetryAfter(timedelta(seconds=3)), "retried"])
    unrelated = AsyncMock(return_value="unrelated")

    async def controlled_sleep(_delay: float) -> None:
        retry_sleep_started.set()
        await release_retry.wait()

    with (
        patch("ccgram.telegram_rate_limiter.asyncio.sleep", controlled_sleep),
        patch("ccgram.telegram_rate_limiter.random.uniform", return_value=0),
    ):
        retrying_task = asyncio.create_task(
            limiter.process_request(
                callback=retrying,
                args=(),
                kwargs={},
                endpoint="editForumTopic",
                data={"chat_id": 1001},
                rate_limit_args=None,
            )
        )
        await asyncio.wait_for(retry_sleep_started.wait(), timeout=0.5)
        unrelated_task = asyncio.create_task(
            limiter.process_request(
                callback=unrelated,
                args=(),
                kwargs={},
                endpoint="sendMessage",
                data={"chat_id": 1002},
                rate_limit_args=None,
            )
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(unrelated_task), timeout=0.1)
        finally:
            release_retry.set()
            await asyncio.gather(retrying_task, unrelated_task)

    assert result == "unrelated"
    unrelated.assert_awaited_once()
    assert retrying.await_count == 2
