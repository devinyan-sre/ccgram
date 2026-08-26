"""Provider lifecycle and delivery diagnostics Telegram commands."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError

from .. import session_query, topic_routing, window_query
from ..config import config
from ..delivery_outbox import delivery_outbox
from ..handoff_context import generate_handoff_context, transcript_tail_offset
from ..i18n import t
from ..multiplexer import multiplexer as tmux_manager
from ..providers import (
    detect_provider_from_pane,
    detect_provider_from_transcript_path,
)
from ..provider_handoff import HandoffResult, handoff_provider
from ..session import session_manager
from ..session_map import read_session_map_raw, session_map_prefix
from ..session_monitor import get_active_monitor
from ..telegram_client import PTBTelegramClient
from ..topic_naming import reserve_topic_name
from ..window_state_ports import identity_state, lifecycle_state
from .callback_data import CB_HANDOFF, CB_PARK, CB_WAKE
from .callback_helpers import get_thread_id, user_owns_window
from .callback_registry import register
from .last_reply import deliver_text
from .messaging_pipeline.message_sender import safe_edit, safe_reply
from .messaging_pipeline.message_queue import queue_snapshot
from .polling.polling_state import lifecycle_strategy
from .status.topic_emoji import format_topic_name_for_mode

if TYPE_CHECKING:
    from telegram import CallbackQuery
    from telegram.ext import ContextTypes

_PROVIDERS = ("claude", "codex", "gemini", "pi", "shell")
_MAX_REPLAY = 10
logger = structlog.get_logger()


def build_handoff_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build provider replacement choices, including context transfer."""
    rows = [
        [
            InlineKeyboardButton(
                "Codex",
                callback_data=f"{CB_HANDOFF}{window_id}:codex:0"[:64],
            ),
            InlineKeyboardButton(
                t("Codex + context"),
                callback_data=f"{CB_HANDOFF}{window_id}:codex:1"[:64],
            ),
        ],
        [
            InlineKeyboardButton(
                "Claude", callback_data=f"{CB_HANDOFF}{window_id}:claude:0"[:64]
            ),
            InlineKeyboardButton(
                "Gemini", callback_data=f"{CB_HANDOFF}{window_id}:gemini:0"[:64]
            ),
            InlineKeyboardButton(
                "Pi", callback_data=f"{CB_HANDOFF}{window_id}:pi:0"[:64]
            ),
        ],
        [
            InlineKeyboardButton(
                t("Park topic"), callback_data=f"{CB_PARK}{window_id}"[:64]
            )
        ],
    ]
    return InlineKeyboardMarkup(rows)


def build_auth_failure_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Recovery actions shown after a provider authentication failure."""
    return build_handoff_keyboard(window_id)


def _resolve_bound(update: Update) -> tuple[int, int, str] | None:
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return None
    thread_id = get_thread_id(update)
    if thread_id is None:
        return None
    window_id = topic_routing.resolve_window(user.id, thread_id)
    if not window_id:
        return None
    return user.id, thread_id, window_id


async def _run_handoff(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    provider_name: str,
    include_context: bool,
) -> HandoffResult:
    context_prompt = (
        await generate_handoff_context(window_id)
        if include_context and provider_name != "shell"
        else ""
    )
    return await handoff_provider(
        user_id=user_id,
        thread_id=thread_id,
        old_window_id=window_id,
        target_provider=provider_name,
        context_prompt=context_prompt,
    )


def _result_text(result: HandoffResult) -> str:
    if not result.success:
        return f"❌ {result.message}"
    context = t(" Context was transferred.") if result.context_sent else ""
    return t("✅ Switched this topic to {provider} ({window}).{context}").format(
        provider=result.provider_name,
        window=result.new_window_id,
        context=context,
    )


async def _sync_topic_name(
    client: PTBTelegramClient,
    user_id: int,
    thread_id: int,
    result: HandoffResult,
) -> None:
    """Apply a committed lifecycle name to the existing Telegram topic."""
    if not result.success or not result.window_name:
        return
    try:
        await client.edit_forum_topic(
            chat_id=topic_routing.resolve_chat(user_id, thread_id),
            message_thread_id=thread_id,
            name=format_topic_name_for_mode(
                result.window_name,
                identity_state.get_approval_mode(result.new_window_id),
            ),
        )
    except TelegramError as exc:
        logger.warning(
            "Lifecycle operation succeeded but topic rename failed: thread=%s error=%s",
            thread_id,
            str(exc),
        )


async def handoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/handoff <provider> [context]`` — replace provider atomically."""
    resolved = _resolve_bound(update)
    if resolved is None or not update.message:
        if update.message:
            await safe_reply(update.message, t("❌ Use /handoff inside a bound topic."))
        return
    user_id, thread_id, window_id = resolved
    args = (update.message.text or "").split()[1:]
    if not args:
        await safe_reply(
            update.message,
            t(
                "Choose the replacement provider. The old session is kept until "
                "the new one is ready."
            ),
            reply_markup=build_handoff_keyboard(window_id),
        )
        return
    provider_name = args[0].lower()
    if provider_name not in _PROVIDERS:
        await safe_reply(update.message, t("❌ Unknown provider."))
        return
    include_context = any(arg.lower() in ("context", "--context") for arg in args[1:])
    await safe_reply(
        update.message,
        t("⏳ Starting {provider} and verifying it…").format(provider=provider_name),
    )
    result = await _run_handoff(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        provider_name=provider_name,
        include_context=include_context,
    )
    await _sync_topic_name(PTBTelegramClient(context.bot), user_id, thread_id, result)
    await safe_reply(update.message, _result_text(result))


async def autoname_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/autoname`` — opt an existing topic into provider-aware naming."""
    resolved = _resolve_bound(update)
    if resolved is None or not update.message:
        if update.message:
            await safe_reply(
                update.message, t("❌ Use /autoname inside a bound topic.")
            )
        return
    user_id, thread_id, window_id = resolved
    view = window_query.view_window(window_id)
    if view is None or not view.cwd:
        await safe_reply(update.message, t("❌ No state exists for this window."))
        return
    provider_name = view.provider_name or "shell"
    async with reserve_topic_name(
        view.cwd,
        provider_name,
        replacing_window_id=window_id,
        force_automatic=True,
    ) as reserved_name:
        window = await tmux_manager.find_window_by_id(window_id)
        renamed = window is None or await tmux_manager.rename_window(
            window_id, reserved_name.name
        )
    if not renamed:
        await safe_reply(update.message, t("❌ Could not apply the automatic name."))
        return
    session_manager.set_display_name(window_id, reserved_name.name)
    session_manager.set_window_auto_named(window_id, value=True)
    result = HandoffResult(
        True,
        window_id,
        new_window_id=window_id,
        provider_name=provider_name,
        window_name=reserved_name.name,
    )
    await _sync_topic_name(PTBTelegramClient(context.bot), user_id, thread_id, result)
    await safe_reply(
        update.message,
        t("✅ Automatic topic name applied: {name}").format(name=reserved_name.name),
    )


async def _park(user_id: int, thread_id: int, window_id: str) -> tuple[bool, str]:
    window = await tmux_manager.find_window_by_id(window_id)
    if window is None:
        lifecycle_state.set_parked(window_id, value=True)
        lifecycle_strategy.mark_dead_notified(user_id, thread_id, window_id)
        return True, t("Topic is already parked. Use /wake to restart it.")
    lifecycle_state.set_parked(window_id, value=True)
    if not await tmux_manager.kill_window(window_id):
        lifecycle_state.set_parked(window_id, value=False)
        return False, t("Could not stop the session.")
    # Keep the thread binding and WindowState as the durable wake record.
    lifecycle_strategy.mark_dead_notified(user_id, thread_id, window_id)
    return True, t("Topic parked. History and project binding were preserved.")


async def park_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/park`` — stop compute while preserving topic history and binding."""
    resolved = _resolve_bound(update)
    if resolved is None or not update.message:
        if update.message:
            await safe_reply(update.message, t("❌ Use /park inside a bound topic."))
        return
    ok, text = await _park(*resolved)
    await safe_reply(update.message, ("✅ " if ok else "❌ ") + text)


async def wake_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/wake [provider]`` — restart a parked topic in the same project."""
    resolved = _resolve_bound(update)
    if resolved is None or not update.message:
        if update.message:
            await safe_reply(update.message, t("❌ Use /wake inside a bound topic."))
        return
    user_id, thread_id, window_id = resolved
    args = (update.message.text or "").split()[1:]
    current = identity_state.get_provider_name(window_id) or "codex"
    provider_name = args[0].lower() if args else current
    if provider_name not in _PROVIDERS:
        await safe_reply(update.message, t("❌ Unknown provider."))
        return
    lifecycle_strategy.clear_dead_notification(user_id, thread_id)
    await safe_reply(
        update.message,
        t("⏳ Waking with {provider}…").format(provider=provider_name),
    )
    result = await _run_handoff(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        provider_name=provider_name,
        include_context=False,
    )
    await safe_reply(update.message, _result_text(result))


def _diagnostic_problems(
    *,
    alive: bool,
    state_provider: str,
    detected_provider: str,
    transcript_provider: str,
    session_id: str,
    tracked: bool = True,
) -> list[str]:
    problems: list[str] = []
    if not alive:
        problems.append("window is parked/dead")
    if detected_provider and state_provider and detected_provider != state_provider:
        problems.append("foreground provider differs from state")
    if transcript_provider and transcript_provider != state_provider:
        problems.append("transcript provider differs from state")
    if alive and state_provider != "shell" and not session_id:
        problems.append("no session binding")
    elif alive and state_provider != "shell" and not tracked:
        problems.append("session is not tracked by the delivery monitor")
    return problems


def _file_size(path: str) -> int:
    if not path:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


async def _diagnose(window_id: str) -> str:
    view = window_query.view_window(window_id)
    if view is None:
        return t("❌ No state exists for this window.")
    window = await tmux_manager.find_window_by_id(window_id)
    foreground = await tmux_manager.foreground(window_id) if window else None
    detected = (
        await detect_provider_from_pane(
            window.pane_current_command, window_id=window_id
        )
        if window
        else ""
    )
    raw = await read_session_map_raw() or {}
    mapped = raw.get(f"{session_map_prefix()}{window_id}", {})
    mapped = mapped if isinstance(mapped, dict) else {}
    transcript = str(view.transcript_path or mapped.get("transcript_path", ""))
    transcript_provider = detect_provider_from_transcript_path(transcript)
    file_size = _file_size(transcript)
    session_id = str(mapped.get("session_id") or view.session_id or "")
    delivered = 0
    tracked_found = False
    monitor = get_active_monitor()
    if monitor and session_id:
        tracked = monitor.state.get_session(session_id)
        if tracked:
            tracked_found = True
            delivered = tracked.delivered_byte_offset
    lag = max(0, file_size - delivered)
    problems = _diagnostic_problems(
        alive=window is not None,
        state_provider=view.provider_name,
        detected_provider=detected,
        transcript_provider=transcript_provider,
        session_id=session_id,
        tracked=tracked_found or not session_id,
    )
    health = "✅ consistent" if not problems else "⚠ " + "; ".join(problems)
    lifecycle = "parked" if lifecycle_state.is_parked(window_id) else "active"
    process = "dead"
    if foreground:
        process = f"pid {foreground.pid} · {' '.join(foreground.argv[:3])}"
    return "\n".join(
        [
            f"Topic diagnostic · `{window_id}`",
            f"Health: {health}",
            f"Lifecycle: {lifecycle}",
            f"Provider: state `{view.provider_name or '-'}` · detected `{detected or '-'}`",
            f"Process: {process}",
            f"Session: `{session_id or '-'}`",
            f"Transcript: `{transcript or '-'}`",
            f"Delivery: file {file_size} bytes · committed {delivered} · lag {lag}",
            f"CWD: `{view.cwd}`",
        ]
    )


async def diag_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/diag`` — show topic/window/session/delivery consistency."""
    resolved = _resolve_bound(update)
    if resolved is None or not update.message:
        if update.message:
            await safe_reply(update.message, t("❌ Use /diag inside a bound topic."))
        return
    await safe_reply(update.message, await _diagnose(resolved[2]))


async def ops_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/ops`` — summarize delivery health without opening server logs."""
    user = update.effective_user
    if not update.message or not user or not config.is_user_allowed(user.id):
        return
    topic_queues, queued, unfinished = queue_snapshot()
    pending, retrying = delivery_outbox.snapshot()
    monitor = get_active_monitor()
    tracked = len(monitor.state.tracked_sessions) if monitor else 0
    lag = 0
    stalled = 0
    if monitor:
        lag = sum(
            max(0, item.last_byte_offset - item.delivered_byte_offset)
            for item in monitor.state.tracked_sessions.values()
        )
        stalled = len(monitor._delivery_lag_alerted)
    health = "✅ healthy" if not pending and not lag else "⚠ attention needed"
    await safe_reply(
        update.message,
        "\n".join(
            [
                f"ccgram ops · {health}",
                f"Sessions: {tracked} tracked · {stalled} stalled",
                f"Delivery: {lag} bytes lag · {pending} durable pending",
                f"Queues: {topic_queues} topics · {queued} queued · {unfinished} active",
                f"Retries: {retrying}",
            ]
        ),
    )


async def replay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/replay [count]`` — safely resend recent assistant text."""
    resolved = _resolve_bound(update)
    if resolved is None or not update.message:
        if update.message:
            await safe_reply(update.message, t("❌ Use /replay inside a bound topic."))
        return
    user_id, thread_id, window_id = resolved
    args = (update.message.text or "").split()[1:]
    try:
        count = int(args[0]) if args else 3
    except ValueError:
        count = 3
    count = min(_MAX_REPLAY, max(1, count))
    messages, _ = await session_query.get_recent_messages(
        window_id, start_byte=transcript_tail_offset(window_id)
    )
    replies = [
        str(message.get("text") or "").strip()
        for message in messages
        if message.get("role") == "assistant"
        and message.get("content_type") == "text"
        and str(message.get("text") or "").strip()
    ][-count:]
    if not replies:
        await safe_reply(
            update.message, t("No assistant replies are available to replay.")
        )
        return
    client = PTBTelegramClient(context.bot)
    chat_id = topic_routing.resolve_chat(user_id, thread_id)
    await deliver_text(
        client,
        chat_id,
        thread_id,
        window_id,
        t("🔁 Replay") + "\n\n" + "\n\n——\n\n".join(replies),
    )


async def _callback_handoff(
    query: CallbackQuery,
    user_id: int,
    thread_id: int,
    window_id: str,
    provider_name: str,
    include_context: bool,
    client: PTBTelegramClient,
) -> None:
    await query.answer()
    await safe_edit(
        query,
        t("⏳ Starting {provider} and verifying it…").format(provider=provider_name),
    )
    result = await _run_handoff(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        provider_name=provider_name,
        include_context=include_context,
    )
    await _sync_topic_name(client, user_id, thread_id, result)
    await safe_edit(query, _result_text(result), reply_markup=None)


@register(CB_HANDOFF, CB_PARK, CB_WAKE)
async def _dispatch(  # noqa: PLR0911
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not user:
        return
    thread_id = get_thread_id(update)
    if thread_id is None:
        await query.answer("Use inside a topic", show_alert=True)
        return
    data = query.data
    if data.startswith(CB_HANDOFF):
        payload = data[len(CB_HANDOFF) :]
        try:
            window_id, provider_name, context_flag = payload.rsplit(":", 2)
        except ValueError:
            await query.answer("Bad handoff request", show_alert=True)
            return
        if not user_owns_window(user.id, window_id) or provider_name not in _PROVIDERS:
            await query.answer("Not your session", show_alert=True)
            return
        await _callback_handoff(
            query,
            user.id,
            thread_id,
            window_id,
            provider_name,
            context_flag == "1",
            PTBTelegramClient(context.bot),
        )
        return
    if data.startswith(CB_PARK):
        window_id = data[len(CB_PARK) :]
        if not user_owns_window(user.id, window_id):
            await query.answer("Not your session", show_alert=True)
            return
        ok, text = await _park(user.id, thread_id, window_id)
        await query.answer()
        await safe_edit(query, ("✅ " if ok else "❌ ") + text, reply_markup=None)
        return
    payload = data[len(CB_WAKE) :]
    try:
        window_id, provider_name = payload.rsplit(":", 1)
    except ValueError:
        await query.answer("Bad wake request", show_alert=True)
        return
    if not user_owns_window(user.id, window_id) or provider_name not in _PROVIDERS:
        await query.answer("Not your session", show_alert=True)
        return
    lifecycle_strategy.clear_dead_notification(user.id, thread_id)
    await _callback_handoff(
        query,
        user.id,
        thread_id,
        window_id,
        provider_name,
        False,
        PTBTelegramClient(context.bot),
    )
