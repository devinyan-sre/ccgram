"""In-process metrics registry — dependency-free Prometheus/OpenMetrics text.

Mirrors the zero-dependency ethos of :mod:`sd_notify`: rather than pull in
``prometheus_client`` we hand-roll the small slice of the exposition format we
need (Counter / Gauge / Histogram) and render it as Prometheus text on demand.
The :data:`registry` singleton owns every metric; :func:`render` serialises the
current values for the ``GET /metrics`` endpoint (see :mod:`metrics_server`).

All mutation goes through a single lock so sync call sites (queue worker, poll
loop, Telegram send layer) can update counters without caring which thread or
task they run on. Reads (``render``) take the same lock for a consistent
snapshot.

Metric objects are declared at module import (see the ``# --- metric
declarations`` block) and imported by call sites, e.g.::

    from .metrics import TELEGRAM_API

    TELEGRAM_API.inc(method="send_message", outcome="ok")

Naming follows Prometheus conventions: ``ccgram_<subsystem>_<unit>[_total]``.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence


# Above this magnitude float→int round-tripping is lossy, so fall back to repr.
_INT_RENDER_LIMIT = 1e15


def _fmt_value(value: float) -> str:
    """Render a float the way Prometheus expects (ints without a trailing .0)."""
    if value == int(value) and abs(value) < _INT_RENDER_LIMIT:
        return str(int(value))
    return repr(value)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels_key(
    labelnames: Sequence[str], labels: Mapping[str, str]
) -> tuple[str, ...]:
    """Order label values by declared labelname order; missing → empty string."""
    return tuple(str(labels.get(name, "")) for name in labelnames)


def _render_labels(labelnames: Sequence[str], key: Sequence[str]) -> str:
    if not labelnames:
        return ""
    # strict=True: _labels_key always pads to len(labelnames), so a length
    # mismatch here is a programming error worth surfacing loudly.
    parts = [
        f'{name}="{_escape_label(val)}"'
        for name, val in zip(labelnames, key, strict=True)
    ]
    return "{" + ",".join(parts) + "}"


class _Metric:
    """Common metadata (name/help/type/labelnames) shared by all metric kinds."""

    kind = ""

    def __init__(
        self, name: str, help_text: str, labelnames: Sequence[str] = ()
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _header(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} {self.kind}",
        ]

    def render(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError


class Counter(_Metric):
    """Monotonic counter. Rendered with the conventional ``_total`` suffix."""

    kind = "counter"

    def __init__(
        self, name: str, help_text: str, labelnames: Sequence[str] = ()
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("Counter.inc amount must be non-negative")
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        lines = self._header()
        metric = f"{self.name}_total"
        if not items:
            # A labelled metric with no observations has no series yet; only an
            # unlabelled one has a well-defined zero to report.
            if not self.labelnames:
                lines.append(f"{metric} 0")
            return lines
        for key, value in items:
            lines.append(
                f"{metric}{_render_labels(self.labelnames, key)} {_fmt_value(value)}"
            )
        return lines


class Gauge(_Metric):
    """Value that can go up and down (queue depth, tracked-session count)."""

    kind = "gauge"

    def __init__(
        self, name: str, help_text: str, labelnames: Sequence[str] = ()
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        lines = self._header()
        if not items:
            # Same rule as Counter: no labelled series until first observation.
            if not self.labelnames:
                lines.append(f"{self.name} 0")
            return lines
        for key, value in items:
            lines.append(
                f"{self.name}{_render_labels(self.labelnames, key)} {_fmt_value(value)}"
            )
        return lines


# Default latency buckets (seconds) — tuned for Telegram/LLM/tick latencies.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


class Histogram(_Metric):
    """Cumulative histogram over a fixed bucket schedule."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(buckets))
        # per-labelset: list of cumulative-per-bucket counts, sum, count
        self._counts: dict[tuple[str, ...], list[float]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = [0.0] * (len(self.buckets) + 1)  # +1 for +Inf
                self._counts[key] = counts
                self._sums[key] = 0.0
            self._sums[key] += value
            placed = False
            for i, upper in enumerate(self.buckets):
                if value <= upper:
                    counts[i] += 1
                    placed = True
                    break
            if not placed:
                counts[len(self.buckets)] += 1  # +Inf bucket

    def render(self) -> list[str]:
        with self._lock:
            keys = sorted(self._counts)
            snapshot = {k: (list(self._counts[k]), self._sums[k]) for k in keys}
        lines = self._header()
        bucket_labels = [*(str(b) for b in self.buckets), "+Inf"]
        for key in keys:
            counts, total = snapshot[key]
            cumulative = 0.0
            base = _render_labels(self.labelnames, key)
            for i, le in enumerate(bucket_labels):
                cumulative += counts[i]
                inner = base[1:-1] + "," if base else ""
                lines.append(
                    f'{self.name}_bucket{{{inner}le="{le}"}} {_fmt_value(cumulative)}'
                )
            lines.append(f"{self.name}_sum{base} {_fmt_value(total)}")
            lines.append(f"{self.name}_count{base} {_fmt_value(cumulative)}")
        return lines


class Registry:
    """Owns every metric object; renders the full exposition text."""

    def __init__(self) -> None:
        self._metrics: list[_Metric] = []

    def register(self, metric: _Metric) -> _Metric:
        self._metrics.append(metric)
        return metric

    def counter(
        self, name: str, help_text: str, labelnames: Sequence[str] = ()
    ) -> Counter:
        return self.register(Counter(name, help_text, labelnames))  # type: ignore[return-value]

    def gauge(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> Gauge:
        return self.register(Gauge(name, help_text, labelnames))  # type: ignore[return-value]

    def histogram(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        return self.register(Histogram(name, help_text, labelnames, buckets))  # type: ignore[return-value]

    def render(self) -> str:
        blocks: Iterable[list[str]] = (m.render() for m in self._metrics)
        body = "\n".join("\n".join(block) for block in blocks)
        return body + "\n" if body else ""

    def reset(self) -> None:
        """Test hook: drop all registered metrics."""
        self._metrics.clear()


registry = Registry()


def render() -> str:
    """Render the whole registry as Prometheus text exposition."""
    return registry.render()


# --- metric declarations -------------------------------------------------
# Declared once here and imported by call sites. Keep names stable — they are
# a public operational contract (dashboards/alerts key on them).

# Telegram Bot API calls, labelled by method + outcome (ok/error/flood).
TELEGRAM_API = registry.counter(
    "ccgram_telegram_api_requests",
    "Telegram Bot API calls by method and outcome",
    ("method", "outcome"),
)
# Flood-control (HTTP 429) hits, labelled by method.
TELEGRAM_FLOOD = registry.counter(
    "ccgram_telegram_flood_control",
    "Telegram flood-control (429) responses by method",
    ("method",),
)

# Outbound message queue.
QUEUE_DEPTH = registry.gauge(
    "ccgram_queue_depth",
    "Current outbound queue depth, per user",
    ("user",),
)
QUEUE_TASKS = registry.counter(
    "ccgram_queue_tasks",
    "Outbound queue tasks processed, by outcome (sent/dropped/failed)",
    ("outcome",),
)
QUEUE_SHED = registry.counter(
    "ccgram_queue_shed",
    "Outbound queue tasks shed due to backpressure",
    ("user",),
)

# Status polling loop.
POLL_CYCLES = registry.counter(
    "ccgram_poll_cycles",
    "Status-poll loop cycles, by outcome (done/error)",
    ("outcome",),
)
POLL_DURATION = registry.histogram(
    "ccgram_poll_cycle_seconds",
    "Status-poll loop cycle duration in seconds",
)

# Session monitor.
SESSIONS_TRACKED = registry.gauge(
    "ccgram_sessions_tracked",
    "Number of sessions currently tracked by the session monitor",
)
MONITOR_BYTES = registry.counter(
    "ccgram_monitor_bytes_read",
    "Transcript bytes read incrementally by the session monitor",
)

# LLM / transcription helpers.
LLM_DURATION = registry.histogram(
    "ccgram_llm_request_seconds",
    "LLM/transcription request duration in seconds, by provider + kind",
    ("kind", "provider"),
)
LLM_REQUESTS = registry.counter(
    "ccgram_llm_requests",
    "LLM/transcription requests by kind, provider and outcome",
    ("kind", "provider", "outcome"),
)

# Topic/window lifecycle (P0-3 observability).
TOPIC_CREATE = registry.counter(
    "ccgram_topic_create",
    "Topic/window creation attempts by outcome (ok/error)",
    ("outcome",),
)

# Operator alerts.
OPERATOR_ALERTS = registry.counter(
    "ccgram_operator_alerts",
    "Operator alerts by severity and delivery outcome",
    ("severity", "outcome"),
)


# --- cross-cutting helpers ----------------------------------------------


@contextlib.contextmanager
def track_poll_cycle() -> Iterator[None]:
    """Time one status-poll loop cycle and record its outcome.

    Packaged as a context manager so ``polling_coordinator`` — a deliberately
    thin orchestrator held to a line ceiling by its fitness test — can adopt
    this cross-cutting concern with a single ``with`` statement instead of
    inlining timing and counter bookkeeping.
    """
    started = time.monotonic()
    try:
        yield
    except BaseException:
        POLL_CYCLES.inc(outcome="error")
        raise
    else:
        POLL_DURATION.observe(time.monotonic() - started)
        POLL_CYCLES.inc(outcome="done")
