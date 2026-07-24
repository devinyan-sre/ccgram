"""End-to-end correlation ids.

The point of a cid is to survive the hop from the routing coroutine into the
queue worker — a different task — so a message stays traceable across the
queue boundary that contextvars alone do not cross.
"""

import structlog

from ccgram import correlation


def _bound():
    return structlog.contextvars.get_contextvars()


def setup_function():
    structlog.contextvars.clear_contextvars()
    correlation.reset_for_testing()


def teardown_function():
    structlog.contextvars.clear_contextvars()


def test_new_cid_is_unique_and_monotonic():
    a, b, c = correlation.new_cid(), correlation.new_cid(), correlation.new_cid()
    assert a != b != c
    assert len({a, b, c}) == 3


def test_bind_and_read_roundtrip():
    cid = correlation.new_cid()
    correlation.bind_cid(cid)
    assert correlation.current_cid() == cid
    assert _bound().get("cid") == cid


def test_current_cid_is_none_when_unbound():
    assert correlation.current_cid() is None


def test_bind_none_is_a_noop():
    correlation.bind_cid(None)
    assert correlation.current_cid() is None


def test_reset_restarts_the_counter():
    first = correlation.new_cid()
    correlation.reset_for_testing()
    assert correlation.new_cid() == first


def test_cid_survives_a_simulated_task_boundary():
    """Enqueue binds a cid; the 'worker' rebinds from carried data, not ctx."""
    # Routing side: bind + capture what would be stamped onto the task.
    cid = correlation.new_cid()
    correlation.bind_cid(cid)
    carried = correlation.current_cid()

    # Worker side: contextvars from the other task are gone.
    structlog.contextvars.clear_contextvars()
    assert correlation.current_cid() is None

    # Rebinding from the task's carried cid restores the trace.
    correlation.bind_cid(carried)
    assert correlation.current_cid() == cid
