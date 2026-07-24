"""Metrics registry + /metrics endpoint contract.

The exposition text is an operational contract (dashboards and alerts key on
these names and shapes), so these tests pin the rendered format rather than
just the in-memory values.
"""

from aiohttp.test_utils import TestClient, TestServer

from ccgram.metrics import Counter, Gauge, Histogram, Registry
from ccgram.metrics_server import build_app


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


# --- Counter -------------------------------------------------------------


def test_counter_renders_zero_when_never_incremented():
    reg = Registry()
    reg.counter("ccgram_thing", "A thing")
    assert _lines(reg.render()) == [
        "# HELP ccgram_thing A thing",
        "# TYPE ccgram_thing counter",
        "ccgram_thing_total 0",
    ]


def test_labelled_counter_emits_no_series_before_first_observation():
    """A labelled metric has no well-defined zero — only HELP/TYPE until used."""
    reg = Registry()
    reg.counter("ccgram_api", "API calls", ("method",))
    assert _lines(reg.render()) == [
        "# HELP ccgram_api API calls",
        "# TYPE ccgram_api counter",
    ]


def test_labelled_gauge_emits_no_series_before_first_observation():
    reg = Registry()
    reg.gauge("ccgram_depth", "Depth", ("user",))
    assert _lines(reg.render()) == [
        "# HELP ccgram_depth Depth",
        "# TYPE ccgram_depth gauge",
    ]


def test_counter_accumulates_and_uses_total_suffix():
    reg = Registry()
    c = reg.counter("ccgram_thing", "A thing")
    c.inc()
    c.inc(2)
    assert "ccgram_thing_total 3" in _lines(reg.render())


def test_counter_separates_label_sets():
    reg = Registry()
    c = reg.counter("ccgram_api", "API calls", ("method", "outcome"))
    c.inc(method="send", outcome="ok")
    c.inc(method="send", outcome="ok")
    c.inc(method="send", outcome="error")
    out = _lines(reg.render())
    assert 'ccgram_api_total{method="send",outcome="ok"} 2' in out
    assert 'ccgram_api_total{method="send",outcome="error"} 1' in out


def test_counter_rejects_negative_increment():
    c = Counter("x", "x")
    try:
        c.inc(-1)
    except ValueError:
        return
    raise AssertionError("negative inc must raise")


def test_counter_escapes_label_values():
    reg = Registry()
    c = reg.counter("ccgram_api", "API calls", ("method",))
    c.inc(method='we"ird\\')
    assert 'ccgram_api_total{method="we\\"ird\\\\"} 1' in _lines(reg.render())


# --- Gauge ---------------------------------------------------------------


def test_gauge_set_inc_dec():
    reg = Registry()
    g = reg.gauge("ccgram_depth", "Depth")
    g.set(5)
    g.inc(2)
    g.dec(3)
    assert "ccgram_depth 4" in _lines(reg.render())


def test_gauge_is_per_label_set():
    reg = Registry()
    g = reg.gauge("ccgram_depth", "Depth", ("user",))
    g.set(3, user="1")
    g.set(7, user="2")
    out = _lines(reg.render())
    assert 'ccgram_depth{user="1"} 3' in out
    assert 'ccgram_depth{user="2"} 7' in out


# --- Histogram -----------------------------------------------------------


def test_histogram_buckets_are_cumulative():
    h = Histogram("ccgram_lat", "Latency", buckets=(1.0, 5.0))
    for value in (0.5, 2.0, 9.0):
        h.observe(value)
    out = h.render()
    assert 'ccgram_lat_bucket{le="1.0"} 1' in out
    assert 'ccgram_lat_bucket{le="5.0"} 2' in out
    assert 'ccgram_lat_bucket{le="+Inf"} 3' in out
    assert "ccgram_lat_count 3" in out
    assert "ccgram_lat_sum 11.5" in out


def test_histogram_boundary_value_lands_in_its_bucket():
    """``le`` is inclusive: an observation equal to the bound counts in it."""
    h = Histogram("ccgram_lat", "Latency", buckets=(1.0,))
    h.observe(1.0)
    out = h.render()
    assert 'ccgram_lat_bucket{le="1.0"} 1' in out
    assert 'ccgram_lat_bucket{le="+Inf"} 1' in out


def test_histogram_merges_labels_with_le():
    h = Histogram("ccgram_lat", "Latency", ("kind",), buckets=(1.0,))
    h.observe(0.5, kind="llm")
    out = h.render()
    assert 'ccgram_lat_bucket{kind="llm",le="1.0"} 1' in out
    assert 'ccgram_lat_sum{kind="llm"} 0.5' in out


# --- Registry ------------------------------------------------------------


def test_registry_render_ends_with_newline():
    reg = Registry()
    reg.counter("ccgram_thing", "A thing").inc()
    assert reg.render().endswith("\n")


def test_empty_registry_renders_empty_string():
    assert Registry().render() == ""


# --- HTTP endpoints ------------------------------------------------------


async def test_metrics_endpoint_serves_prometheus_text():
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
        assert resp.status == 200
        assert "version=0.0.4" in resp.headers["Content-Type"]
        body = await resp.text()
    # Declared metrics are always present, even at zero.
    assert "ccgram_telegram_api_requests" in body


async def test_healthz_reports_ok_when_healthy():
    app = build_app(health_check=lambda: True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.text() == "ok"


async def test_healthz_reports_503_when_unhealthy():
    app = build_app(health_check=lambda: False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 503


async def test_healthz_treats_raising_check_as_unhealthy():
    def boom() -> bool:
        raise RuntimeError("wedged")

    app = build_app(health_check=boom)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 503


async def test_healthz_defaults_to_ok_without_a_check():
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200


# --- Gauge/Counter used by call sites are declared ------------------------


def test_declared_metric_names_are_stable():
    """These names are a public operational contract — renaming breaks dashboards."""
    from ccgram import metrics

    expected = {
        "ccgram_telegram_api_requests",
        "ccgram_telegram_flood_control",
        "ccgram_queue_depth",
        "ccgram_queue_tasks",
        "ccgram_queue_shed",
        "ccgram_poll_cycles",
        "ccgram_poll_cycle_seconds",
        "ccgram_sessions_tracked",
        "ccgram_monitor_bytes_read",
        "ccgram_llm_request_seconds",
        "ccgram_llm_requests",
        "ccgram_topic_create",
        "ccgram_operator_alerts",
    }
    rendered = metrics.render()
    for name in expected:
        assert name in rendered, f"missing declared metric {name}"


def test_track_poll_cycle_records_success():
    from ccgram import metrics

    before = metrics.registry.render()
    with metrics.track_poll_cycle():
        pass
    after = metrics.registry.render()
    assert 'ccgram_poll_cycles_total{outcome="done"}' in after
    assert after != before


def test_track_poll_cycle_counts_error_and_reraises():
    from ccgram import metrics

    try:
        with metrics.track_poll_cycle():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    else:
        raise AssertionError("track_poll_cycle must not swallow exceptions")
    assert 'ccgram_poll_cycles_total{outcome="error"}' in metrics.registry.render()


def test_metric_kinds_are_what_call_sites_assume():
    from ccgram import metrics

    assert isinstance(metrics.QUEUE_DEPTH, Gauge)
    assert isinstance(metrics.QUEUE_TASKS, Counter)
    assert isinstance(metrics.POLL_DURATION, Histogram)
