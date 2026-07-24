"""Outbound queue backpressure.

The queue was unbounded: a flooding user or a slow Telegram could grow it
without limit. Shedding is tiered because the two task kinds do not cost the
same to lose — a status bubble is superseded within ~1s, agent output is not.
"""

import asyncio

import pytest

from ccgram.config import config
from ccgram.handlers.messaging_pipeline import message_queue as mq


@pytest.fixture
def _restore_limit():
    saved = config.queue_max_depth
    yield
    config.queue_max_depth = saved


def _queue(depth: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    for _ in range(depth):
        queue.put_nowait(object())
    return queue


def test_nothing_shed_below_the_soft_cap(_restore_limit):
    config.queue_max_depth = 10
    queue = _queue(5)
    assert mq._shed(queue, 1, "status") is False
    assert mq._shed(queue, 1, "content") is False


def test_status_is_shed_at_the_soft_cap(_restore_limit):
    config.queue_max_depth = 10
    assert mq._shed(_queue(10), 1, "status") is True


def test_content_survives_the_soft_cap(_restore_limit):
    """Agent output must not be dropped just because status is backing up."""
    config.queue_max_depth = 10
    assert mq._shed(_queue(10), 1, "content") is False


def test_content_is_shed_only_past_the_hard_cap(_restore_limit):
    config.queue_max_depth = 10
    assert mq._shed(_queue(19), 1, "content") is False
    assert mq._shed(_queue(20), 1, "content") is True


def test_zero_limit_disables_shedding_entirely(_restore_limit):
    config.queue_max_depth = 0
    assert mq._shed(_queue(10_000), 1, "status") is False
    assert mq._shed(_queue(10_000), 1, "content") is False


def test_shedding_is_counted(_restore_limit):
    from ccgram import metrics

    config.queue_max_depth = 1
    mq._shed(_queue(5), 4242, "status")
    assert 'ccgram_queue_shed_total{user="4242"}' in metrics.registry.render()
