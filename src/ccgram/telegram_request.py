"""Telegram request helpers for resilient long polling."""

import asyncio
import time
from collections.abc import Callable

import httpx
import structlog
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

from .metrics import TELEGRAM_API

logger = structlog.get_logger()

# Minimum interval between reset warnings during a sustained outage.
# Without this, every failed poll (~5s apart) emits a warning, flooding logs.
_RESET_WARN_INTERVAL_S: float = 30.0
_WARN_AFTER_CONSECUTIVE_RESETS: int = 3


def _api_method_name(args: tuple, kwargs: dict) -> str:
    """Extract the Bot API method name from a request URL, for metric labels.

    The URL's trailing path segment is the method (``.../bot<TOKEN>/getUpdates``).
    Only that segment is returned — the bot token is an earlier segment and
    must never reach a label value.
    """
    url = kwargs.get("url") or (args[0] if args else "")
    if not isinstance(url, str) or not url:
        return "unknown"
    return url.rsplit("/", 1)[-1] or "unknown"


class ResilientPollingHTTPXRequest(HTTPXRequest):
    """Reset the polling HTTP client after transient transport failures.

    PTB uses a dedicated request object for ``getUpdates`` with a single
    connection. If that connection gets stuck in a bad proxy/tunnel state,
    subsequent polls can queue behind it forever. Rebuilding the client after a
    timeout/network failure gives the polling loop a fresh pool on the next
    retry.

    Concurrent failures sharing a stale client rebuild it once. Isolated
    resets log at info; only a sustained outage crosses the warning threshold.
    """

    def __init__(
        self,
        *args,
        on_success: Callable[[], None] | None = None,
        request_name: str = "Telegram",
        **kwargs,
    ) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._on_success = on_success
        self.request_name = request_name
        self._last_reset_warn_ts: float | None = None
        self._consecutive_resets = 0
        self._reset_lock = asyncio.Lock()
        self._client_close_tasks: set[asyncio.Task[None]] = set()

    async def _reset_client(
        self, *, failed_client: httpx.AsyncClient, reason: str
    ) -> bool:
        async with self._reset_lock:
            if self._client is not failed_client:
                return False
            self._client = self._build_client()

        close_task = asyncio.create_task(failed_client.aclose())
        self._client_close_tasks.add(close_task)

        def discard_close_task(task: asyncio.Task[None]) -> None:
            self._client_close_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        close_task.add_done_callback(discard_close_task)
        try:
            async with asyncio.timeout(1.0):
                await asyncio.shield(close_task)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, RuntimeError, OSError, httpx.HTTPError) as exc:
            logger.debug(
                "Ignoring error while closing stale polling client after %s: %s",
                reason,
                exc,
            )
        return True

    def _should_warn_for_reset(self, now: float) -> bool:
        """Warn only for a sustained outage, at most once per interval."""
        if self._consecutive_resets < _WARN_AFTER_CONSECUTIVE_RESETS:
            return False
        if (
            self._last_reset_warn_ts is None
            or now - self._last_reset_warn_ts >= _RESET_WARN_INTERVAL_S
        ):
            self._last_reset_warn_ts = now
            return True
        return False

    async def post(self, *args, **kwargs):  # type: ignore[override]
        result = await super().post(*args, **kwargs)
        self._last_reset_warn_ts = None
        self._consecutive_resets = 0
        if self._on_success is not None:
            self._on_success()
        return result

    async def do_request(self, *args, **kwargs):  # type: ignore[override]
        method = _api_method_name(args, kwargs)
        failed_client = self._client
        try:
            result = await super().do_request(*args, **kwargs)
        except (TimedOut, NetworkError) as exc:
            TELEGRAM_API.inc(method=method, outcome=exc.__class__.__name__)
            if await self._reset_client(
                failed_client=failed_client, reason=exc.__class__.__name__
            ):
                self._consecutive_resets += 1
                log = (
                    logger.warning
                    if self._should_warn_for_reset(time.monotonic())
                    else logger.info
                )
                log(
                    "Reset Telegram HTTP client (%s) after %s: %s",
                    self.request_name,
                    exc.__class__.__name__,
                    exc,
                )
            raise
        else:
            TELEGRAM_API.inc(method=method, outcome="ok")
            return result
