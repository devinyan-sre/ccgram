"""systemd integration — sd_notify protocol + watchdog heartbeat.

Speaks the sd_notify(3) datagram protocol directly over ``$NOTIFY_SOCKET``
(no external dependency). Under ``Type=notify`` + ``WatchdogSec`` the bot
sends ``READY=1`` once bootstrapped, then ``WATCHDOG=1`` heartbeats at half
the watchdog interval — gated on a health check, so a wedged runtime stops
the heartbeat and systemd restarts the service. Outside systemd (no
``$NOTIFY_SOCKET``) every call is a no-op.

Key functions: notify(), watchdog_interval(), start_watchdog(), stop_watchdog().
"""

import asyncio
import os
import socket
import structlog
from collections.abc import Callable

from .utils import task_done_callback

logger = structlog.get_logger()

_watchdog_task: asyncio.Task | None = None


def notify(state: str) -> bool:
    """Send one sd_notify state string (e.g. ``READY=1``). Best-effort.

    Returns True when the datagram was sent, False when not running under
    systemd or on any socket error (never raises).
    """
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr:
        return False
    if addr.startswith("@"):
        # Abstract-namespace socket: leading '@' encodes a NUL byte.
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        logger.warning("sd_notify send failed for state %r", state)
        return False
    return True


def watchdog_interval() -> float | None:
    """Return the systemd watchdog interval in seconds, or None when unarmed.

    Honors ``WATCHDOG_PID``: when set and not our PID, the watchdog belongs
    to another process and we must not ping it.
    """
    usec = os.environ.get("WATCHDOG_USEC", "")
    if not usec:
        return None
    wd_pid = os.environ.get("WATCHDOG_PID", "")
    if wd_pid and wd_pid != str(os.getpid()):
        return None
    try:
        interval = int(usec) / 1_000_000
    except ValueError:
        return None
    return interval if interval > 0 else None


def start_watchdog(health_check: Callable[[], bool]) -> asyncio.Task | None:
    """Start the WATCHDOG=1 heartbeat task; None when the watchdog is unarmed.

    Pings at half the configured interval while ``health_check()`` is True.
    An unhealthy runtime skips the ping (logged) so systemd's WatchdogSec
    expires and the service is restarted.
    """
    global _watchdog_task

    interval = watchdog_interval()
    if interval is None or not os.environ.get("NOTIFY_SOCKET"):
        return None
    if _watchdog_task is not None and not _watchdog_task.done():
        return _watchdog_task

    async def _heartbeat() -> None:
        period = interval / 2
        logger.info("systemd watchdog armed: pinging every %.1fs", period)
        while True:
            await asyncio.sleep(period)
            if health_check():
                notify("WATCHDOG=1")
            else:
                logger.warning(
                    "Runtime unhealthy — skipping watchdog ping "
                    "(systemd will restart the service)"
                )

    _watchdog_task = asyncio.create_task(_heartbeat())
    _watchdog_task.add_done_callback(task_done_callback)
    return _watchdog_task


def stop_watchdog() -> None:
    """Cancel the heartbeat task. Idempotent."""
    global _watchdog_task
    if _watchdog_task is not None:
        _watchdog_task.cancel()
        _watchdog_task = None
