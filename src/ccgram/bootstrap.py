"""Application bootstrap — wires post_init and post_shutdown lifecycle.

`bot.py` defines the PTB ``Application`` factory + lifecycle delegates;
the actual wiring (provider commands, runtime callbacks, session
monitor, status polling, mini-app) lives here as named functions so
each step is independently testable.

Ordering invariant: ``wire_runtime_callbacks`` must run before
``start_session_monitor`` because the monitor dispatches Stop events
to the registered Stop callback, and an unwired callback raises after
F2.6.

Module-level state (``session_monitor``, ``_status_poll_task``) is
created in post_init and torn down in post_shutdown — kept here, not
in ``bot.py``, so the lifecycle delegates stay one-liners.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import structlog
from telegram.error import TelegramError

from . import health
from .cc_commands import register_commands
from .config import config
from .handlers.commands import setup_menu_refresh_job
from .handlers.hook_events import dispatch_hook_event
from .handlers.messaging_pipeline.message_queue import (
    is_session_delivery_drained,
    shutdown_workers,
)
from .handlers.messaging_pipeline.message_routing import handle_new_message
from .handlers.polling.polling_coordinator import status_poll_loop
from .handlers.shell import register_approval_callback, show_command_approval
from .handlers.topics.topic_orchestration import (
    adopt_unbound_windows as _adopt_unbound_windows,
)
from .handlers.topics.topic_orchestration import (
    handle_new_window as _handle_new_window,
)
from .multiplexer import get_multiplexer, install_multiplexer, multiplexer
from .providers import get_provider
from .session import session_manager
from . import sd_notify
from .telegram_client import PTBTelegramClient
from .session_monitor import (
    NewMessage,
    NewWindowEvent,
    SessionMonitor,
    clear_active_monitor,
    get_active_monitor,
    set_active_monitor,
)
from .utils import task_done_callback

if TYPE_CHECKING:
    from telegram.ext import Application

    from .providers.base import HookEvent

logger = structlog.get_logger()

session_monitor: SessionMonitor | None = None
_status_poll_task: asyncio.Task[None] | None = None
_callbacks_wired = False
_metrics_runner: object | None = None


def install_global_exception_handler() -> None:
    """Install the asyncio last-resort exception handler."""
    asyncio.get_running_loop().set_exception_handler(_global_exception_handler)


def _global_exception_handler(
    _loop: asyncio.AbstractEventLoop, ctx: dict[str, object]
) -> None:
    """Last-resort handler for uncaught exceptions in asyncio tasks."""
    exc = ctx.get("exception")
    msg = ctx.get("message", "Unhandled exception in event loop")
    if isinstance(exc, BaseException):
        logger.error(
            "asyncio exception handler: %s",
            msg,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        logger.error("asyncio exception handler: %s", msg)


async def register_provider_commands(application: Application) -> None:
    """Register the default provider's BotCommand list and schedule menu refresh."""
    default_provider = get_provider()
    try:
        await register_commands(application.bot, provider=default_provider)
    except TelegramError:
        logger.warning("Failed to register bot commands at startup, will retry later")
    setup_menu_refresh_job(application)
    # Lazy: daily_digest pulls handler messaging paths; wire only at bootstrap
    from .handlers.daily_digest import setup_daily_digest_job

    setup_daily_digest_job(application)


async def run_startup_permission_check(application: Application) -> None:
    """Verify the bot's Manage-Topics right in each target chat.

    Surfaces a missing admin right at boot (log + operator DM) instead of
    silently failing auto-topic-creation later. Best-effort: any failure is
    logged and swallowed so it can never block startup.
    """
    # Lazy: operator_alerts pulls telegram_client + i18n; only needed here.
    from .operator_alerts import check_group_permissions, check_operator_reachable

    # Lazy: thread_router proxy, read only for extra bound group chats.
    from .thread_router import thread_router

    client = PTBTelegramClient(application.bot)

    # 1. Operator DM reachability — independent of any bound group topics, so
    #    it runs even when no group is configured. Surfaces the "can't initiate
    #    conversation" case at boot instead of silently failing at alert time.
    try:
        await check_operator_reachable(client)
    except Exception:  # noqa: BLE001 — advisory step, never fatal
        logger.warning("Operator reachability check failed", exc_info=True)

    # 2. Manage-Topics right in each target group chat.
    chat_ids: set[int] = set()
    if config.group_id is not None:
        chat_ids.add(config.group_id)
    chat_ids.update(cid for cid in thread_router.group_chat_ids.values() if cid < 0)
    if not chat_ids:
        return
    try:
        bot_id = application.bot.id
    except RuntimeError, AttributeError:
        logger.debug("Bot id unavailable; skipping startup permission check")
        return
    try:
        await check_group_permissions(client, sorted(chat_ids), bot_id)
    except Exception:  # noqa: BLE001 — advisory step, never fatal
        logger.warning("Startup permission check failed", exc_info=True)


def verify_hooks_installed() -> None:
    """Warn if managed hooks are missing for the default provider."""
    provider = get_provider()
    if not provider.capabilities.supports_hook:
        return
    provider_name = provider.capabilities.name
    if provider_name != "claude":
        if provider.capabilities.hook_install_managed_by_ccgram:
            # DEBUG (not INFO/WARNING): Codex/Gemini fall back to transcript-scan
            # discovery when hooks are absent, so this is an opt-in latency tip,
            # not a degraded state — it should not greet every startup at INFO.
            logger.debug(
                "%s hooks can improve status tracking. Run: ccgram hook --provider %s --install",
                provider_name,
                provider_name,
            )
        return

    # Lazy: hook module is the Claude-Code subprocess entry point;
    # importing it eagerly drags `utils`/IO costs into bootstrap even
    # when the active provider has no hooks.
    # Lazy: hook helpers used only during the hook-verify step
    from .hooks.install import _claude_settings_file, get_installed_events

    settings_file = _claude_settings_file()
    if not settings_file.exists():
        logger.warning(
            "Claude Code hooks not installed (%s missing). Run: ccgram hook --install",
            settings_file,
        )
        return

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError, OSError:
        logger.warning("Claude Code hooks not installed. Run: ccgram hook --install")
        return

    events = get_installed_events(settings)
    missing = [e for e, ok in events.items() if not ok]
    if missing:
        logger.warning(
            "Claude Code hooks incomplete — %d missing: %s. Run: ccgram hook --install",
            len(missing),
            ", ".join(missing),
        )


def wire_multiplexer() -> None:
    """Install the configured multiplexer backend as the module-level proxy.

    Selects the backend from ``config.multiplexer_name`` (``CCGRAM_MULTIPLEXER``,
    default tmux). Must run before the session monitor / status polling start so
    callers that use the ``multiplexer`` proxy forward to a wired backend.
    Idempotent — re-installs the same cached backend on repeat calls.
    """
    backend = get_multiplexer(config.multiplexer_name)
    install_multiplexer(backend)
    logger.info("Multiplexer backend wired: %s", backend.capabilities.name)


async def ensure_multiplexer_session() -> None:
    """Ensure the active backend's session/server is reachable before polling.

    tmux creates/finds the session; herdr verifies the socket is alive and the
    pinned protocol version matches (raising on mismatch). Runs once at startup
    via the seam so a misconfigured backend fails loudly here rather than later
    as silent ``None`` returns in the polling loop.

    An unreachable backend is fatal but not a bug: log one actionable line and
    exit cleanly. ``SystemExit`` (unlike a plain exception) is caught by PTB's
    ``run_polling`` and triggers a graceful shutdown, so the user sees the error
    instead of a traceback.
    """
    try:
        await multiplexer.ensure_session()
    except Exception as exc:
        logger.error(
            "Multiplexer '%s' is not available: %s. "
            "Make sure it is installed and running, then start ccgram again.",
            config.multiplexer_name,
            exc,
        )
        raise SystemExit(1) from exc


def wire_runtime_callbacks() -> None:
    """Wire module-level callbacks that break cross-subsystem direct imports.

    Idempotent — safe to call multiple times. Must run before
    ``start_session_monitor`` — the monitor dispatches approval prompts to
    ``register_approval_callback``, which raises if not wired.
    """
    global _callbacks_wired

    if _callbacks_wired:
        return

    register_approval_callback(show_command_approval)
    _callbacks_wired = True


async def start_session_monitor(application: Application) -> SessionMonitor:
    """Build the SessionMonitor, set its callbacks, and start polling.

    Raises ``RuntimeError`` if ``wire_runtime_callbacks`` has not run —
    the monitor would dispatch Stop events to an unwired callback.
    """
    global session_monitor

    if not _callbacks_wired:
        raise RuntimeError(
            "wire_runtime_callbacks() must run before start_session_monitor()"
        )

    monitor = SessionMonitor()
    set_active_monitor(monitor)

    # Lazy: telegram_client wraps PTB Bot; bootstrap is otherwise free of
    # PTB types, so loading the adapter here keeps cold imports clean.

    client = PTBTelegramClient(application.bot)

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, client)

    monitor.set_message_callback(message_callback)

    async def new_window_callback(event: NewWindowEvent) -> None:
        await _handle_new_window(event, client)

    monitor.set_new_window_callback(new_window_callback)

    async def hook_event_callback(event: HookEvent) -> None:
        await dispatch_hook_event(event, client)

    monitor.set_hook_event_callback(hook_event_callback)

    # Crash-recovery commit barrier: monitor offsets are persisted as
    # delivered only once the serving queues drain.
    monitor.set_delivery_drained_callback(is_session_delivery_drained)

    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")
    return monitor


def start_status_polling(application: Application) -> asyncio.Task[None]:
    """Spawn the status-polling background task."""
    global _status_poll_task

    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    _status_poll_task.add_done_callback(task_done_callback)
    logger.info("Status polling task started")
    return _status_poll_task


def start_event_stream(application: Application) -> object | None:
    """Start the push event-stream consumer on event-stream backends (herdr).

    No-op on backends without ``capabilities.supports_event_stream`` (tmux).
    Returns the monitor (or None) so callers/tests can inspect it.
    """
    if not multiplexer.capabilities.supports_event_stream:
        return None
    # Lazy: keep the event-stream consumer (and its handler graph) out of the
    # cold-import path; only event-stream backends ever load it.
    from .event_stream_monitor import EventStreamMonitor, set_active_event_stream

    # Lazy: thread_router proxy, used only to seed the bound-window set.
    from .thread_router import thread_router

    def _bound_window_ids() -> set[str]:
        return {wid for _u, _t, wid in thread_router.iter_thread_bindings()}

    monitor = EventStreamMonitor(PTBTelegramClient(application.bot), _bound_window_ids)
    monitor.start()
    set_active_event_stream(monitor)
    logger.info("Event-stream consumer started")
    return monitor


async def bootstrap_application(application: Application) -> None:
    """Run the full post_init sequence in the prescribed order."""
    install_global_exception_handler()
    wire_multiplexer()
    await ensure_multiplexer_session()
    await register_provider_commands(application)
    await session_manager.resolve_stale_ids()
    await _adopt_unbound_windows(PTBTelegramClient(application.bot))
    await run_startup_permission_check(application)
    verify_hooks_installed()
    wire_runtime_callbacks()
    await start_session_monitor(application)
    start_status_polling(application)
    start_event_stream(application)

    # Lazy: main imports bot at top, bot imports bootstrap; hoisting forms
    # main → bot → bootstrap → main on cold import.
    # Lazy: bootstrap ↔ main cycle
    from .main import start_miniapp_if_enabled

    await start_miniapp_if_enabled()
    await start_metrics_if_enabled()

    # Arm error-rate alerting now that the bot can DM the operator.
    if config.error_alerts_enabled:
        # Lazy: operator_alerts pulls telegram_client + i18n.
        from .operator_alerts import set_error_alert_client

        set_error_alert_client(PTBTelegramClient(application.bot))

    # systemd integration: signal readiness and arm the health-gated
    # watchdog heartbeat (both no-ops outside Type=notify units).
    sd_notify.notify("READY=1")
    sd_notify.start_watchdog(_runtime_healthy)


def _runtime_healthy() -> bool:
    """Health gate for the systemd watchdog heartbeat.

    Two layers:

    1. *Liveness* — the session-monitor and status-polling tasks exist and
       have not finished. Catches a crashed loop.
    2. *Forward progress* — each loop stamped a completed cycle within
       ``CCGRAM_HEALTH_STALL_SEC``. Catches a loop that is wedged but alive
       (blocked on a hung syscall, or spinning without completing a cycle),
       which layer 1 cannot see.

    Either check failing withholds the heartbeat so systemd restarts the
    service. A component that has never reported progress is treated as
    healthy so startup does not trip the gate before the first cycle lands.
    """
    monitor = get_active_monitor()
    if monitor is None or monitor._task is None or monitor._task.done():
        return False
    if _status_poll_task is None or _status_poll_task.done():
        return False

    threshold = config.health_stall_seconds
    if threshold <= 0:  # progress check disabled — liveness only
        return True
    for component in (health.SESSION_MONITOR, health.STATUS_POLL):
        if health.is_stalled(component, threshold):
            logger.warning(
                "Runtime stalled — no forward progress",
                component=component,
                seconds=health.seconds_since_progress(component),
                threshold=threshold,
            )
            return False
    return True


async def start_metrics_if_enabled() -> None:
    """Start the metrics/health listener when ``CCGRAM_METRICS_PORT`` is set.

    Idempotent. Failures are logged and swallowed — an unbindable metrics port
    must never take the bot down. ``/healthz`` is backed by the same
    :func:`_runtime_healthy` gate the systemd watchdog uses, so blackbox probes
    and deploy health gates agree with systemd.
    """
    global _metrics_runner

    if _metrics_runner is not None:
        return
    if config.metrics_port <= 0:
        return

    try:
        # Lazy: metrics_server pulls aiohttp; keep it off the import path for
        # deployments that leave the listener disabled.
        from .metrics_server import start_server

        _metrics_runner = await start_server(
            host=config.metrics_host,
            port=config.metrics_port,
            health_check=_runtime_healthy,
        )
        logger.info(
            "Metrics server started",
            host=config.metrics_host,
            port=config.metrics_port,
        )
    except OSError as exc:
        logger.error("Metrics server failed to bind", error=str(exc))
        _metrics_runner = None


async def stop_metrics_if_enabled() -> None:
    """Tear down the metrics listener if this process started one."""
    global _metrics_runner

    if _metrics_runner is None:
        return
    try:
        # Lazy: mirrors the start path — aiohttp stays off the cold import path.
        from .metrics_server import stop_server

        await stop_server(_metrics_runner)  # type: ignore[arg-type]
        logger.info("Metrics server stopped")
    except Exception as exc:  # noqa: BLE001 — teardown must never mask shutdown
        logger.warning("Metrics server teardown failed", error=str(exc))
    finally:
        _metrics_runner = None


async def shutdown_runtime() -> None:
    """Run the post_shutdown teardown sequence."""
    global _status_poll_task, session_monitor

    sd_notify.notify("STOPPING=1")
    sd_notify.stop_watchdog()

    # Lazy: disarm the error-alert sink so a stopped bot can't DM.
    from .operator_alerts import set_error_alert_client

    set_error_alert_client(None)

    if _status_poll_task is not None:
        _status_poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _status_poll_task
        _status_poll_task = None
        logger.info("Status polling stopped")

    if session_monitor is not None:
        session_monitor.stop()
        logger.info("Session monitor stopped")
        session_monitor = None
    clear_active_monitor()

    # Lazy: event-stream consumer is only loaded on event-stream backends.
    from .event_stream_monitor import get_active_event_stream, set_active_event_stream

    event_stream = get_active_event_stream()
    if event_stream is not None:
        event_stream.stop()
        set_active_event_stream(None)
        logger.info("Event-stream consumer stopped")

    await shutdown_workers()

    # Lazy: main → bot → bootstrap cycle (same as start path).
    from .main import stop_miniapp_if_enabled

    await stop_miniapp_if_enabled()
    await stop_metrics_if_enabled()

    session_manager.flush_state()


def reset_for_testing() -> None:
    """Clear bootstrap module state and inner callback registrations.

    Each e2e/integration test that drives ``bootstrap_application`` must
    reset state between runs — F2.6 made the register_*_callbacks fail
    loud on double registration, and bootstrap caches its own
    ``_callbacks_wired`` flag too.
    """
    global _callbacks_wired, session_monitor, _status_poll_task, _metrics_runner

    # Lazy: each module's _reset_*_for_testing hook is only needed by the
    # test harness; production callers never reach reset_for_testing().
    from .handlers.shell import shell_capture

    shell_capture._reset_approval_callback_for_testing()

    _callbacks_wired = False
    session_monitor = None
    _status_poll_task = None
    _metrics_runner = None
    health.reset_for_testing()
    clear_active_monitor()
    sd_notify.stop_watchdog()

    # Stop any event-stream consumer this run started and clear its caches so the
    # supervisor task + module-global state don't leak into the next test.
    # Lazy: event-stream consumer is only loaded on event-stream backends.
    from .event_stream_monitor import get_active_event_stream, set_active_event_stream
    from .multiplexer import agent_status_cache

    event_stream = get_active_event_stream()
    if event_stream is not None:
        event_stream.stop()
        set_active_event_stream(None)
    agent_status_cache.reset()

    # Lazy: clear the error-alert sink + tracker so state can't leak between runs.
    from .operator_alerts import reset_error_alerts_for_testing

    reset_error_alerts_for_testing()
