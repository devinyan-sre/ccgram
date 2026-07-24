"""Standalone metrics/health HTTP listener.

Deliberately independent of the Mini App server: the Mini App is an optional
user-facing feature gated on ``CCGRAM_MINIAPP_BASE_URL``, whereas metrics and
health probes must be available to the operator whenever the bot runs. Enabled
by setting ``CCGRAM_METRICS_PORT`` to a non-zero port (default ``0`` = off);
binds to ``CCGRAM_METRICS_HOST`` (default ``127.0.0.1``) so nothing is exposed
publicly without an explicit reverse proxy.

Routes:
- ``GET /metrics`` — Prometheus text exposition of :mod:`metrics`.
- ``GET /healthz`` — ``200 ok`` / ``503 unhealthy`` from an injected health
  callback (wired to the same gate the systemd watchdog uses), so blackbox
  probes and deploy health gates see exactly what systemd sees.

Both routes are unauthenticated: they expose no session content, and the
listener defaults to loopback.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from aiohttp import web

from .metrics import render

logger = structlog.get_logger()

# Prometheus text exposition content type (version pinned per convention).
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_HEALTH_KEY = web.AppKey("health_check", object)


async def _handle_metrics(_request: web.Request) -> web.Response:
    # body= (not text=) so the pinned Content-Type header is the only one set;
    # aiohttp forbids combining an explicit header with content_type/charset.
    return web.Response(
        body=render().encode("utf-8"),
        headers={"Content-Type": _CONTENT_TYPE},
    )


async def _handle_health(request: web.Request) -> web.Response:
    check = request.app.get(_HEALTH_KEY)
    if check is None:
        return web.Response(text="ok")
    try:
        healthy = bool(check())  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 — a raising probe means unhealthy
        logger.warning("metrics health check raised", error=str(exc))
        healthy = False
    if healthy:
        return web.Response(text="ok")
    return web.Response(text="unhealthy", status=503)


def build_app(health_check: Callable[[], bool] | None = None) -> web.Application:
    """Build the metrics aiohttp application."""
    app = web.Application()
    if health_check is not None:
        app[_HEALTH_KEY] = health_check
    app.router.add_get("/metrics", _handle_metrics)
    app.router.add_get("/healthz", _handle_health)
    return app


async def start_server(
    host: str,
    port: int,
    health_check: Callable[[], bool] | None = None,
) -> web.AppRunner:
    """Start the metrics listener and return its runner."""
    app = build_app(health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


async def stop_server(runner: web.AppRunner) -> None:
    """Tear down a runner returned by :func:`start_server`."""
    await runner.cleanup()
