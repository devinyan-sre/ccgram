"""Voice message handler — download OGG audio, transcribe via Whisper, and present confirm keyboard.

Handles Telegram voice messages by downloading the audio, transcribing it using
the configured Whisper provider, and showing the transcription with a confirm/discard
inline keyboard so the user can review before sending to the agent.

Key handler:
  - handle_voice_message: main entry point for filters.VOICE
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from ...config import config
from ...i18n import t
from ...inbound_store import inbound_store
from ...task_scheduler import task_scheduler
from ...task_focus import consume as consume_task_focus
from ...telegram_client import PTBTelegramClient
from ...thread_router import thread_router
from ...whisper import get_transcriber
from ...whisper.base import TranscriptionResult, WhisperTranscriber
from ..callback_helpers import get_thread_id
from ..group_activation import evaluate_group_activation
from ..messaging_pipeline.message_sender import safe_reply
from ..user_state import VOICE_PENDING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# Max voice file size: 25 MB (Telegram Bot API getFile limit)
_MAX_VOICE_SIZE = 25 * 1024 * 1024


def _build_voice_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Build the confirm/discard inline keyboard for a transcribed voice message."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("✓ Send to agent"),
                    callback_data=f"vc:send:{message_id}",
                ),
                InlineKeyboardButton(
                    t("✗ Discard"),
                    callback_data=f"vc:drop:{message_id}",
                ),
            ]
        ]
    )


async def _download_voice(message: Message, file_id: str) -> bytes | None:
    """Download voice audio from Telegram. Returns bytes or None on error."""
    try:
        file = await message.get_bot().get_file(file_id)
        audio_bytearray = await file.download_as_bytearray()
        return bytes(audio_bytearray)
    except TelegramError as e:
        logger.warning("Failed to download voice message: %s", e)
        await safe_reply(message, t("❌ Failed to download voice message."))
        return None


async def _get_transcriber_or_reply(message: Message) -> WhisperTranscriber | None:
    """Resolve the configured transcriber and surface user-facing errors."""
    try:
        transcriber = get_transcriber()
    except (ValueError, RuntimeError) as e:
        await safe_reply(message, f"❌ {e}")
        return None

    if transcriber is None:
        await safe_reply(
            message,
            t(
                "⚠️ Voice transcription is not configured. Set"
                " CCGRAM_WHISPER_PROVIDER to enable it.\n\nSupported providers:"
                " openai, groq"
            ),
        )
        return None

    return transcriber


async def _transcribe_audio(
    message: Message, transcriber: WhisperTranscriber, audio_bytes: bytes
) -> TranscriptionResult | None:
    """Transcribe audio bytes. Returns TranscriptionResult or None on error."""
    try:
        return await transcriber.transcribe(audio_bytes, "voice.ogg")
    except (ValueError, RuntimeError) as e:
        await safe_reply(message, f"❌ {e}")
        return None


async def _send_confirm_message(
    message: Message,
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    window_id: str,
) -> None:
    """Send transcription with confirm/discard keyboard in a single message.

    Uses the original voice message_id as the callback reference so the keyboard
    is included on first send (no edit_reply_markup needed).
    """
    keyboard = _build_voice_keyboard(message.message_id)
    confirm_msg = await safe_reply(
        message,
        t("🎤 Transcribed:\n\n{text}").format(text=text),
        reply_markup=keyboard,
    )
    if confirm_msg is None:
        return

    if context.user_data is not None:
        key = (confirm_msg.chat.id, message.message_id)
        context.user_data.setdefault(VOICE_PENDING, {})[key] = (
            text,
            window_id,
            message.message_id,
        )


async def _deliver_transcription(
    message: Message,
    text: str,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show confirmation, or auto-send when explicitly enabled."""
    if config.voice_autosend is not True:
        await _send_confirm_message(message, text, context, window_id)
        return

    await safe_reply(message, t("🎤 Transcribed:\n\n{text}").format(text=text))
    # Lazy: the PTB adapter is needed only by the opt-in autosend path.
    from ...telegram_client import PTBTelegramClient

    # Lazy: voice_callbacks owns the shared provider-aware delivery path.
    from .voice_callbacks import send_transcribed_text

    # Lazy: acknowledgement machinery otherwise pulls in the send pipeline.
    from ..messaging_pipeline.message_sender import ack_reaction

    client = PTBTelegramClient(message.get_bot())
    success, error = await send_transcribed_text(
        client, user_id, thread_id, window_id, text, source_message=message
    )
    if success:
        await ack_reaction(client, message.chat.id, message.message_id)
    else:
        await safe_reply(message, f"❌ {error or t('Failed to send')}")


async def handle_voice_message(  # noqa: C901, PLR0911 - explicit fail-closed media pipeline
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle incoming voice messages: transcribe and present confirm keyboard."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.voice:
        return

    if not config.is_user_allowed(user.id):
        await safe_reply(message, t("You are not authorized to use this bot."))
        return

    # Voice has no caption, so in mention-gated groups it must be sent as a
    # reply to one of the bot's messages. Unaddressed group voice is ignored.
    activation = evaluate_group_activation(message, PTBTelegramClient(context.bot), "")
    if not activation.accepted:
        return

    thread_id = get_thread_id(update)
    window_id = thread_router.resolve_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            message,
            t(
                "⚠ Topic not bound — send a text message first to pick a "
                "directory, then re-record.\n"
                "\U0001f4ac Voice messages aren't queued."
            ),
        )
        return
    assert thread_id is not None
    focused_task_id = consume_task_focus(message.chat.id, thread_id, user.id)
    if focused_task_id:
        focused = next(
            (
                row
                for row in task_scheduler.views(
                    chat_id=message.chat.id, thread_id=thread_id
                )
                if row.user_id == user.id
                and row.task_id.upper() == focused_task_id
                and row.state != "queued"
            ),
            None,
        )
        if focused is not None:
            window_id = focused.window_id
    reply = message.reply_to_message
    if reply is not None:
        linked = inbound_store.resolve_message(
            chat_id=message.chat.id,
            thread_id=thread_id,
            user_id=user.id,
            message_id=reply.message_id,
        )
        if linked is not None and linked.task_id:
            active = next(
                (
                    row
                    for row in task_scheduler.views(
                        chat_id=message.chat.id, thread_id=thread_id
                    )
                    if row.user_id == user.id
                    and row.task_id.upper() == linked.task_id.upper()
                    and row.state != "queued"
                ),
                None,
            )
            if active is not None:
                window_id = active.window_id
    owned = [
        row
        for row in task_scheduler.views(chat_id=message.chat.id, thread_id=thread_id)
        if row.user_id == user.id and row.state != "queued"
    ]
    if len(owned) > 1 and reply is None and focused_task_id is None:
        await safe_reply(
            message,
            "⚠️ 你有多个活动任务，语音没有明确归属。请回复对应任务消息后重新发送语音。",
        )
        return

    voice = message.voice
    if voice.file_size is not None and voice.file_size > _MAX_VOICE_SIZE:
        size_mb = voice.file_size / (1024 * 1024)
        await safe_reply(
            message,
            t("❌ Voice message too large ({size} MB). Maximum 25 MB.").format(
                size=f"{size_mb:.1f}"
            ),
        )
        return

    transcriber = await _get_transcriber_or_reply(message)
    if transcriber is None:
        return

    audio_bytes = await _download_voice(message, voice.file_id)
    if audio_bytes is None:
        return

    await message.get_bot().send_chat_action(
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        action=ChatAction.TYPING,
    )

    result = await _transcribe_audio(message, transcriber, audio_bytes)
    if result is None:
        return

    if not result.text.strip():
        await safe_reply(message, t("⚠️ Could not transcribe audio (empty result)."))
        return

    await _deliver_transcription(
        message,
        result.text,
        user.id,
        thread_id,
        window_id,
        context,
    )
