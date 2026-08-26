"""Conservative long-idle topic parking.

Only ccgram-created, clean git workspaces with no delivery work are eligible.
The feature is disabled when ``CCGRAM_AUTO_PARK_DAYS=0``.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from . import window_query
from .config import config
from .delivery_outbox import delivery_outbox
from .handlers.messaging_pipeline.message_queue import get_message_queue
from .multiplexer import multiplexer as tmux_manager
from .telegram_client import TelegramClient
from .thread_router import thread_router
from .window_state_ports import lifecycle_state, pane_state

logger = structlog.get_logger()
_notified: set[str] = set()


async def _git_clean(cwd: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd,
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode == 0 and not stdout.strip()
    except OSError:
        return False


async def check_auto_park(client: TelegramClient) -> int:  # noqa: C901
    """Warn or park eligible topics, returning the number parked."""
    if config.auto_park_days <= 0:
        return 0
    now = time.time()
    park_after = config.auto_park_days * 86400
    notice_before = config.auto_park_notice_hours * 3600
    parked = 0
    for user_id, thread_id, window_id in list(thread_router.iter_thread_bindings()):
        view = window_query.view_window(window_id)
        if (
            not view
            or view.origin != "ccgram_created"
            or lifecycle_state.is_parked(window_id)
        ):
            continue
        panes = pane_state.list_pane_projections(window_id)
        last_active = max((pane.last_active_ts for pane in panes), default=0.0)
        if not last_active:
            continue
        idle = now - last_active
        if idle < max(0, park_after - notice_before):
            continue
        queue = get_message_queue(user_id, thread_id)
        if queue and getattr(queue, "_unfinished_tasks", 0):
            continue
        if view.session_id and delivery_outbox.has_pending_session(view.session_id):
            continue
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if idle < park_after:
            if window_id not in _notified:
                await client.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=(
                        "💤 This topic has been idle for a long time and will "
                        f"auto-park in about {config.auto_park_notice_hours}h. "
                        "Send any message to keep it active."
                    ),
                )
                _notified.add(window_id)
            continue
        if not await _git_clean(view.cwd):
            continue
        lifecycle_state.set_parked(window_id, value=True)
        if not await tmux_manager.kill_window(window_id):
            lifecycle_state.set_parked(window_id, value=False)
            continue
        await client.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=(
                "💤 Topic auto-parked after extended inactivity. History and "
                "project binding are preserved; send a message to wake it."
            ),
        )
        _notified.discard(window_id)
        parked += 1
        logger.info(
            "Auto-parked idle topic", window_id=window_id, days=config.auto_park_days
        )
    return parked
