"""Voice transcription callbacks — handle confirm (send to agent) and discard actions.

Handles the inline keyboard callbacks triggered after voice message transcription:
  - vc:send:<msg_id>: Send transcribed text to the bound agent window
  - vc:drop:<msg_id>: Discard the transcription and delete the confirmation message

Key function: handle_voice_callback
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import structlog
from telegram import CallbackQuery, Message, Update
from telegram.error import TelegramError
from ...i18n import t
from ...dispatch_confirmation import dispatch_confirmation
from ...inbound_store import inbound_store
from ...task_scheduler import task_scheduler
from ...providers import get_provider_for_window
from ...telegram_client import PTBTelegramClient
from ...window_query import get_window_provider
from ...multiplexer.window_ops import send_to_window
from ...thread_router import thread_router
from ..callback_data import CB_VOICE
from ..callback_helpers import get_thread_id
from ..callback_registry import register
from ..messaging_pipeline.message_sender import (
    REACT_DONE,
    REACT_SEEN,
    ack_reaction,
    react,
)
from ..task_intake import admit_request, message_ids
from ..user_state import VOICE_PENDING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()
_PENDING_WITH_WINDOW = 2
_PENDING_WITH_ROOT = 3


async def send_transcribed_text(
    client: PTBTelegramClient,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    source_message: Message | None = None,
    source_message_id: int | None = None,
) -> tuple[bool, str | None]:
    """Send transcription through the same provider-aware path as confirmation."""
    admission = None
    inbound_key = ""
    if source_message is not None and thread_id is not None:
        admission = await admit_request(
            window_id=window_id,
            user_id=user_id,
            thread_id=thread_id,
            message=source_message,
            dispatch_text=text,
            lane_id=thread_router.task_id_for_window(window_id) or "default",
            task_id=thread_router.task_id_for_window(window_id),
            source_message_id=source_message_id,
        )
        if admission is None:
            return False, "任务未被调度，语音内容没有发送。"
        chat_id, detected_source_id = message_ids(source_message)
        source_id = source_message_id or detected_source_id
        inbound_key = inbound_store.make_key(chat_id, thread_id, source_id)
        inbound_store.set_state(inbound_key, "dispatching")
        await dispatch_confirmation.mark_written(window_id)
    provider = get_provider_for_window(
        window_id, provider_name=get_window_provider(window_id)
    )
    if provider.capabilities.chat_first_command_path and thread_id is not None:
        # Lazy: shell command handling imports the voice callback registry.
        from ..shell.shell_commands import handle_shell_message

        try:
            await handle_shell_message(client, user_id, thread_id, window_id, text)
        except (OSError, TelegramError) as exc:
            logger.warning("Shell message handling failed: %s", exc)
            return False, t("Failed to send")
        if inbound_key:
            inbound_store.set_state(inbound_key, "forwarded")
        return True, None
    success, error = await send_to_window(window_id, text)
    if inbound_key:
        inbound_store.set_state(inbound_key, "forwarded" if success else "failed")
    if not success:
        dispatch_confirmation.complete(window_id)
    if not success and admission is not None and not admission.continuation:
        await task_scheduler.release_window(window_id, outcome="failed")
    return success, error or None


async def handle_voice_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle voice transcription confirm/discard callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user:
        return

    # Ensure the message is accessible (not expired/deleted)
    if not isinstance(query.message, Message):
        await query.answer(t("Message no longer available"))
        return

    try:
        parts = query.data.split(":", 2)  # ["vc", "send"/"drop", "<msg_id>"]
        action = parts[1]
        message_id = int(parts[2])
    except IndexError, ValueError:
        await query.answer(t("Invalid callback data"))
        return

    if action == "send":
        await _handle_send(query.message, query, user.id, message_id, update, context)
    elif action == "drop":
        await _handle_drop(query.message, query, message_id, context)
    else:
        await query.answer(t("Invalid callback data"))


async def _handle_send(
    msg: Message,
    query: CallbackQuery,
    user_id: int,
    message_id: int,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle vc:send — forward transcribed text to the agent window."""
    pending_store = (
        context.user_data.get(VOICE_PENDING, {}) if context.user_data else {}
    )
    pending_value = pending_store.pop((msg.chat.id, message_id), None)
    if pending_value is None:
        await query.answer(
            t("⚠️ Session expired, resend voice message"), show_alert=True
        )
        return

    thread_id = get_thread_id(update)
    correlated = isinstance(pending_value, tuple) and len(pending_value) in (
        _PENDING_WITH_WINDOW,
        _PENDING_WITH_ROOT,
    )
    source_message_id: int | None = None
    if correlated:
        pending_text = str(pending_value[0])
        raw_window_id = pending_value[1]
        stored_window_id = raw_window_id if isinstance(raw_window_id, str) else None
        if len(pending_value) == _PENDING_WITH_ROOT:
            raw_source_id = pending_value[2]
            if isinstance(raw_source_id, int):
                source_message_id = raw_source_id
    else:
        pending_text, stored_window_id = str(pending_value), None
    window_id = stored_window_id or thread_router.resolve_window_for_thread(
        user_id, thread_id
    )
    if not window_id:
        pending_store[(msg.chat.id, message_id)] = pending_value
        await query.answer(t("⚠️ No session bound."), show_alert=True)
        return

    client = PTBTelegramClient(msg.get_bot())

    # 👀 ack: persistent "I see you" indicator on the original voice message.
    await react(client, msg.chat.id, message_id, REACT_SEEN)

    success, err = await send_transcribed_text(
        client,
        user_id,
        thread_id,
        window_id,
        pending_text,
        source_message=msg if correlated else None,
        source_message_id=source_message_id,
    )

    if success:
        await _ack_delivered(client, msg, query, message_id)
    else:
        pending_store[(msg.chat.id, message_id)] = pending_value
        await query.answer(f"❌ {err}", show_alert=True)


async def _ack_delivered(
    client: PTBTelegramClient, msg: Message, query: CallbackQuery, message_id: int
) -> None:
    """Replace the previous "✓ Sent" toast with a persistent reaction.

    Order matters: REACT_DONE replaces the prior 👀; ack_reaction (if user
    configured ``CCGRAM_ACK_REACTION``) overrides REACT_DONE in turn.
    """
    await react(client, msg.chat.id, message_id, REACT_DONE)
    await ack_reaction(client, msg.chat.id, message_id)
    try:
        await msg.delete()
    except TelegramError as e:
        logger.warning("Failed to delete voice confirm message: %s", e)
    await query.answer()


async def _handle_drop(
    msg: Message,
    query: CallbackQuery,
    message_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle vc:drop — discard the transcription and delete the confirm message."""
    if context.user_data is not None:
        context.user_data.get(VOICE_PENDING, {}).pop((msg.chat.id, message_id), None)

    try:
        await msg.delete()
    except TelegramError as e:
        logger.warning("Failed to delete voice confirm message on discard: %s", e)

    await query.answer(t("Discarded"))


# --- Registry dispatch entry point ---


@register(CB_VOICE)
async def _dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_voice_callback(update, context)
