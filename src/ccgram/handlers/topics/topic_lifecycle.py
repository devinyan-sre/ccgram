"""Topic lifecycle management — autoclose timers, unbound window TTL, probing.

Periodic tasks that manage topic and window lifecycle:
  - Autoclose: expire done/dead topics after configurable timeout
  - Unbound window TTL: kill orphaned tmux windows without topic bindings
  - Topic existence probing: detect deleted Telegram topics via API
  - State pruning: sync display names and remove stale entries
"""

from __future__ import annotations
import time
from typing import TYPE_CHECKING

import structlog
from telegram import Update
from telegram.error import BadRequest, TelegramError
from ... import window_query
from ...config import config
from ...destructive_audit import (
    ACTION_TOPIC_RETIRED,
    ACTION_WINDOW_KILLED_TOPIC_GONE,
    ACTION_WINDOW_KILLED_UNBOUND,
    ACTOR_AUTO,
    record_destructive,
)
from ...destructive_guard import destruction_blocked, outcome_for
from ...i18n import t
from ...session import session_manager
from ...session_map import session_map_prefix
from ...telegram_client import PTBTelegramClient, TelegramClient
from ...thread_router import thread_router
from ...multiplexer import multiplexer as tmux_manager
from ...utils import log_throttled
from ...window_state_ports import lifecycle_state
from ...window_state_store import CCGRAM_CREATED_WINDOW_ORIGIN
from ..cleanup import clear_topic_state
from ..messaging_pipeline.message_sender import is_thread_gone, safe_send
from ..polling.polling_state import (
    lifecycle_strategy,
    terminal_poll_state,
)

if TYPE_CHECKING:
    from telegram.ext import ContextTypes
    from ...multiplexer.base import WindowRef as TmuxWindow

logger = structlog.get_logger()


# ── Autoclose timer management ────────────────────────────────────────────


async def check_autoclose_timers(client: TelegramClient) -> None:
    """Close topics whose done/dead timers have expired."""
    all_topics = lifecycle_strategy.iter_topic_states()
    if not all_topics:
        return

    now = time.monotonic()
    expired: list[tuple[int, int, str]] = []
    for user_id, thread_id, ts in all_topics:
        if ts.autoclose is None:
            continue
        state, entered_at = ts.autoclose
        if state == "done":
            timeout = config.autoclose_done_minutes * 60
        elif state == "dead":
            timeout = config.autoclose_dead_minutes * 60
        else:
            continue
        if timeout > 0 and now - entered_at >= timeout:
            expired.append((user_id, thread_id, state))

    for user_id, thread_id, state in expired:
        await _close_expired_topic(client, user_id, thread_id, state)


# Topics that already got the "archived" notice. A close that fails (e.g. the
# bot lost can_manage_topics) leaves the autoclose timer armed and is retried
# every cycle — without this guard the notice would be re-sent every retry.
# Entries are dropped once the topic is actually retired.
_archive_notified: set[tuple[int, int]] = set()


async def _retire_topic(
    client: TelegramClient, chat_id: int, thread_id: int, *, delete: bool
) -> bool:
    """Close (or delete) a topic. Returns True when it is retired or gone.

    ``delete`` mode keeps the legacy delete-then-close fallback; the default
    close-only path never touches the message history.
    """
    if delete:
        try:
            await client.delete_forum_topic(
                chat_id=chat_id, message_thread_id=thread_id
            )
            return True
        except TelegramError as e:
            if is_thread_gone(e):
                return True
    try:
        await client.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        return True
    except TelegramError as close_err:
        if is_thread_gone(close_err):
            return True
        logger.debug("autoclose_failed", thread_id=thread_id, error=str(close_err))
        return False


async def _close_expired_topic(
    client: TelegramClient, user_id: int, thread_id: int, state: str
) -> None:
    """Retire an expired topic and clean up its state.

    The default action is a non-destructive ``close_forum_topic``: the topic is
    archived and every message stays readable.  ``CCGRAM_AUTOCLOSE_ACTION=delete``
    restores the legacy ``delete_forum_topic`` behaviour — Telegram destroys the
    whole message history along with the topic, so it is opt-in only.
    """
    window_id = thread_router.get_window_for_thread(user_id, thread_id)
    if state == "dead" and window_id is not None:
        live_window = await tmux_manager.find_window_by_id(window_id)
        if live_window is not None:
            lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)
            logger.info(
                "stale_dead_autoclose_cleared",
                thread_id=thread_id,
                user_id=user_id,
                window_id=window_id,
            )
            return

    blocked = destruction_blocked()
    if blocked:
        # Leave the timer armed: once the suspension lapses the sweep
        # re-evaluates against reality instead of an outage's aftermath.
        await record_destructive(
            ACTION_TOPIC_RETIRED,
            actor=ACTOR_AUTO,
            outcome=outcome_for(blocked),
            detail=blocked,
            window_id=window_id,
            thread_id=thread_id,
            user_id=user_id,
        )
        return

    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    delete = config.autoclose_action == "delete"
    if not delete and (user_id, thread_id) not in _archive_notified:
        # Explain the sudden silence before the topic stops accepting messages.
        _archive_notified.add((user_id, thread_id))
        await safe_send(
            client,
            chat_id,
            t(
                "🗄 Session ended — topic archived. History is kept; "
                "reopen the topic to start a new session here."
            ),
            message_thread_id=thread_id,
            disable_notification=True,
        )
    removed = await _retire_topic(client, chat_id, thread_id, delete=delete)
    if removed:
        _archive_notified.discard((user_id, thread_id))
        lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)
        logger.info(
            "auto_removed_topic",
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            action="delete" if delete else "close",
        )
        await record_destructive(
            ACTION_TOPIC_RETIRED,
            actor=ACTOR_AUTO,
            detail=(
                t("Deleted — the topic's whole message history is gone.")
                if delete
                else t("Archived after the {state} timeout.").format(state=state)
            ),
            window_id=window_id,
            thread_id=thread_id,
            user_id=user_id,
        )
        await clear_topic_state(
            user_id,
            thread_id,
            client=client,
            window_id=window_id,
            window_dead=True,
        )
        thread_router.unbind_thread(user_id, thread_id)


# ── Unbound window TTL ────────────────────────────────────────────────────


async def check_unbound_window_ttl(
    live_windows: "list[TmuxWindow] | None" = None,
) -> None:
    """Kill unbound tmux windows whose TTL has expired."""
    timeout = config.autoclose_done_minutes * 60
    if timeout <= 0:
        return

    bound_ids: set[str] = set()
    for _, _, wid in thread_router.iter_thread_bindings():
        bound_ids.add(wid)

    if live_windows is None:
        live_windows = await tmux_manager.list_windows()
    live_ids = {w.window_id for w in live_windows}

    terminal_poll_state.clear_unbound_timers(bound_ids, live_ids)

    now = time.monotonic()
    for w in live_windows:
        if w.window_id in bound_ids:
            # Re-bound: the window is in use again, so a later orphaning should
            # start a fresh TTL rather than inherit the /unbind exemption.
            lifecycle_state.set_user_detached(w.window_id, value=False)
            continue
        if lifecycle_state.is_user_detached(w.window_id):
            # /unbind promised this session keeps running — never reap it.
            terminal_poll_state.clear_unbound_timer(w.window_id)
            continue
        view = window_query.view_window(w.window_id)
        if view is None or view.origin != CCGRAM_CREATED_WINDOW_ORIGIN:
            terminal_poll_state.clear_unbound_timer(w.window_id)
            continue
        ws = terminal_poll_state.get_state(w.window_id)
        if ws.unbound_timer is None:
            terminal_poll_state.set_unbound_timer(w.window_id, now)

    await _kill_expired_unbound(now, timeout)
    _prune_orphaned_poll_state(live_ids, bound_ids)


async def _kill_expired_unbound(now: float, timeout: float) -> None:
    """Find and kill unbound windows past their TTL."""
    expired = terminal_poll_state.get_expired_unbound(now, timeout)
    for wid in expired:
        blocked = destruction_blocked()
        if blocked:
            # Timer stays set — the next cycle after the suspension re-checks.
            await record_destructive(
                ACTION_WINDOW_KILLED_UNBOUND,
                actor=ACTOR_AUTO,
                outcome=outcome_for(blocked),
                detail=blocked,
                window_id=wid,
            )
            continue
        await tmux_manager.kill_window(wid)

        # Lazy: topic_state_registry is wired during bootstrap; importing
        # at top dragged registration side effects into the polling
        # subpackage's import path.
        from ...topic_state_registry import topic_state

        topic_state.clear_window(wid)
        qualified_id = f"{session_map_prefix()}{wid}"
        topic_state.clear_qualified(qualified_id)
        logger.info("auto_killed_unbound_window", window_id=wid)
        await record_destructive(
            ACTION_WINDOW_KILLED_UNBOUND,
            actor=ACTOR_AUTO,
            detail=t("The agent process and any unsaved work in it are gone."),
            window_id=wid,
        )


def _prune_orphaned_poll_state(live_ids: set[str], bound_ids: set[str]) -> None:
    """Remove poll state for windows that are neither live nor bound."""
    for wid in terminal_poll_state.get_orphaned_window_ids(live_ids, bound_ids):
        terminal_poll_state.clear_state(wid)


# ── Display name sync / state pruning ─────────────────────────────────────


async def prune_stale_state(live_windows: "list[TmuxWindow]") -> None:
    """Sync display names and prune orphaned state entries."""
    live_ids = {w.window_id for w in live_windows}
    live_pairs = [(w.window_id, w.window_name) for w in live_windows]
    session_manager.sync_display_names(live_pairs)
    session_manager.prune_stale_state(live_ids)


# ── Topic existence probing ───────────────────────────────────────────────


# Windows whose chat lacks can_pin_messages: the unpin-based probe can never
# succeed there, so disable it permanently (per process) instead of counting it
# as a probe failure (which would suspend deleted-topic detection and re-arm on
# every inbound message). Reset on restart; mirrors _disabled_chats in
# handlers/status/topic_emoji.py.
_probe_pin_disabled: set[str] = set()


async def _confirm_topic_gone(
    client: TelegramClient, chat_id: int, thread_id: int
) -> bool:
    """Authoritatively re-check that a topic is gone before acting on it.

    The unpin-based sweep is a cheap liveness ping, not a verdict — this module
    used to kill a running agent on a *single* ``Topic_id_invalid`` from it,
    while ``sync_command._probe_dead_topics`` documents that only
    ``send_message`` reliably reports a missing thread. Killing a process on
    the weaker signal meant one Telegram hiccup could destroy live work.

    So: send an invisible, silent probe and delete it again. Only a definitive
    "thread is gone" BadRequest counts as confirmation; any other outcome —
    delivery succeeded, network error, rate limit — is inconclusive and returns
    False, leaving the window untouched for a later cycle to re-evaluate.
    """
    try:
        msg = await client.send_message(
            chat_id=chat_id,
            text="​",  # zero-width space — invisible in the topic
            message_thread_id=thread_id,
            disable_notification=True,
        )
    except BadRequest as exc:
        return is_thread_gone(exc)
    except TelegramError:
        return False  # inconclusive — never escalate a transient failure
    # The topic accepted a message, so it exists: the unpin verdict was wrong.
    message_id = getattr(msg, "message_id", None)
    if message_id is not None:
        try:
            await client.delete_message(chat_id, message_id)
        except TelegramError:
            logger.debug("probe_cleanup_failed", thread_id=thread_id)
    return False


async def _handle_probe_topic_gone(
    client: TelegramClient, user_id: int, thread_id: int, wid: str, reason: str
) -> None:
    """Act on an unpin probe that claims the topic is gone.

    Two gates stand between the claim and any destruction: the mass-death
    breaker (an outage is not a reason to reap) and an authoritative re-check
    (the unpin verdict alone has been wrong).
    """
    blocked = destruction_blocked()
    if blocked:
        await record_destructive(
            ACTION_WINDOW_KILLED_TOPIC_GONE,
            actor=ACTOR_AUTO,
            outcome=outcome_for(blocked),
            detail=blocked,
            window_id=wid,
            thread_id=thread_id,
            user_id=user_id,
        )
        return
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    if not await _confirm_topic_gone(client, chat_id, thread_id):
        logger.info(
            "topic_probe_unconfirmed",
            thread_id=thread_id,
            window_id=wid,
            reason=reason,
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    view = window_query.view_window(wid)
    killed = False
    if w and view and view.origin == CCGRAM_CREATED_WINDOW_ORIGIN:
        await tmux_manager.kill_window(w.window_id)
        killed = True
    terminal_poll_state.reset_probe_failures(wid)
    await clear_topic_state(user_id, thread_id, client, window_id=wid)
    thread_router.unbind_thread(user_id, thread_id)
    logger.info(
        "Topic deleted: %s window_id '%s' and unbound thread %d for user %d",
        "killed" if killed else "unbound",
        wid,
        thread_id,
        user_id,
    )
    if killed:
        await record_destructive(
            ACTION_WINDOW_KILLED_TOPIC_GONE,
            actor=ACTOR_AUTO,
            detail=t("The agent process and any unsaved work in it are gone."),
            window_id=wid,
            thread_id=thread_id,
            user_id=user_id,
        )


async def probe_topic_existence(client: TelegramClient) -> None:
    """Probe all bound topics via Telegram API; detect deleted topics."""
    for user_id, thread_id, wid in list(thread_router.iter_thread_bindings()):
        if wid in _probe_pin_disabled or lifecycle_strategy.should_skip_probe(wid):
            continue
        try:
            await client.unpin_all_forum_topic_messages(
                chat_id=thread_router.resolve_chat_id(user_id, thread_id),
                message_thread_id=thread_id,
            )
            terminal_poll_state.reset_probe_failures(wid)
        except TelegramError as e:
            if isinstance(e, BadRequest) and (
                "Topic_id_invalid" in e.message
                or "thread not found" in e.message.lower()
            ):
                await _handle_probe_topic_gone(
                    client, user_id, thread_id, wid, e.message
                )
            elif isinstance(e, BadRequest) and "not enough rights" in e.message.lower():
                _probe_pin_disabled.add(wid)
                logger.info(
                    "Topic probe disabled for window_id '%s': bot lacks pin rights",
                    wid,
                )
            else:
                lifecycle_strategy.record_probe_failure(wid)
                if not lifecycle_strategy.should_skip_probe(wid):
                    log_throttled(
                        logger,
                        f"topic-probe:{wid}",
                        "Topic probe error for %s: %s",
                        wid,
                        e,
                    )


# Telegram topic event handlers.


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — unbind thread but keep the tmux window alive.

    The window becomes "unbound" and is available for rebinding via the window
    picker when a new topic is created. Unbound windows are auto-killed after
    the configured TTL (autoclose_done_minutes) by the status polling loop.
    """
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return

    # Lazy: callback_helpers ↔ topic_lifecycle through bootstrap wiring.
    from ..callback_helpers import get_thread_id

    thread_id = get_thread_id(update)
    if thread_id is None:
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if window_id:
        display = thread_router.get_display_name(window_id)
        await clear_topic_state(
            user.id,
            thread_id,
            PTBTelegramClient(context.bot),
            context.user_data,
            window_id=window_id,
            window_dead=False,
        )
        thread_router.unbind_thread(user.id, thread_id)
        logger.info(
            "Topic closed: window %s unbound (kept alive for rebinding, user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def topic_edited_handler(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and emoji cache.

    Ignores icon-only edits (name is None) and emoji-only changes from the bot
    itself (clean name unchanged after stripping prefixes).
    """
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message or not update.message.forum_topic_edited:
        return

    new_name = update.message.forum_topic_edited.name
    if not new_name:
        return

    # Lazy: same callback_helpers cycle plus status.topic_emoji ↔ topics
    # cycle through emoji refresh callbacks.
    # Lazy: handlers.callback_helpers / handlers.status cycle
    from ..callback_helpers import get_thread_id

    # Lazy: handlers.callback_helpers / handlers.status cycle
    from ..status.topic_emoji import strip_emoji_prefix, update_stored_topic_name

    thread_id = get_thread_id(update)
    if thread_id is None:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    window_id = thread_router.get_window_for_chat_thread(chat_id, thread_id)
    if not window_id:
        logger.debug("Topic edited: no binding (thread=%d)", thread_id)
        return

    clean_name = strip_emoji_prefix(new_name)

    current_display = thread_router.get_display_name(window_id)
    if current_display and strip_emoji_prefix(current_display) == clean_name:
        logger.debug(
            "Topic edited: name unchanged after strip, skipping (thread=%d)", thread_id
        )
        return

    renamed = await tmux_manager.rename_window(window_id, clean_name)
    if renamed:
        session_manager.set_display_name(window_id, clean_name)
        session_manager.set_window_auto_named(window_id, value=False)
        update_stored_topic_name(chat_id, thread_id, clean_name)
        logger.info(
            "Topic renamed: window %s → %r (thread=%d)",
            window_id,
            clean_name,
            thread_id,
        )
