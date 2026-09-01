"""Photo and document handlers for provider-neutral agent file prompts.

Saves uploaded files to `.ccgram-uploads/` in the session's cwd, then sends
the active agent a natural-language message with the relative path so it can read the
file via its Read tool.

Key handlers:
  - handle_photo_message: handles filters.PHOTO
  - handle_document_message: handles filters.Document.ALL
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
from dataclasses import dataclass
import structlog
import re
import unicodedata
from pathlib import Path

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from ..config import config
from ..i18n import t
from ..inbound_store import inbound_store
from ..request_context import clear_window as clear_request_window
from ..task_scheduler import task_scheduler
from ..telegram_client import PTBTelegramClient
from ..window_query import view_window
from ..user_time import now_display
from ..multiplexer.window_ops import send_to_window
from ..thread_router import thread_router
from .callback_helpers import get_thread_id
from .group_activation import evaluate_group_activation
from .messaging_pipeline.message_sender import ack_reaction, safe_reply
from .task_intake import admit_request, message_ids
from .text.member_lanes import ensure_member_lane
from ..utils import task_done_callback

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_UPLOAD_DIR = ".ccgram-uploads"

_MAX_FILENAME_BYTES = 200

# Max file size in bytes (50 MB — Telegram Bot API limit for getFile)
_MAX_FILE_SIZE = 50 * 1024 * 1024

_FILENAME_PUNCT = frozenset("._-")

# Control characters to strip from captions (keep \n and \t)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_MAX_CAPTION_LEN = 500


@dataclass(slots=True)
class _AlbumItem:
    message: Message
    window_id: str
    user_id: int
    thread_id: int
    rel_path: str
    caption: str


_album_buffers: dict[tuple[int, int, int, str], list[_AlbumItem]] = {}
_album_flush_tasks: dict[tuple[int, int, int, str], asyncio.Task[None]] = {}


def _keep_filename_char(char: str) -> bool:
    """Keep letters/digits from any script, combining marks, and safe punctuation."""
    return (
        char in _FILENAME_PUNCT
        or char.isalnum()
        or unicodedata.category(char).startswith("M")
    )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate to a UTF-8 byte limit without splitting a character."""
    return text.encode()[:max_bytes].decode(errors="ignore")


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename while preserving safe Unicode script characters."""
    name = Path(name).name
    name = unicodedata.normalize("NFC", name)
    name = "".join(char if _keep_filename_char(char) else "_" for char in name)
    if not name.strip("."):
        name = "unnamed"
    if len(name.encode()) > _MAX_FILENAME_BYTES:
        suffix = Path(name).suffix
        if len(suffix.encode()) >= _MAX_FILENAME_BYTES:
            suffix = _truncate_utf8(suffix, 10)
        stem_bytes = _MAX_FILENAME_BYTES - len(suffix.encode())
        name = _truncate_utf8(Path(name).stem, stem_bytes) + suffix
    return name or "unnamed"


def _sanitize_caption(text: str) -> str:
    """Strip control characters, collapse newlines to spaces, and limit length."""
    cleaned = _CONTROL_CHAR_RE.sub("", text)
    # Replace newlines with spaces to prevent tmux keystroke splitting
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    return cleaned[:_MAX_CAPTION_LEN]


def _validate_dest_path(dest: Path, upload_path: Path) -> bool:
    """Ensure dest resolves within upload_path (path traversal guard)."""
    try:
        dest.resolve().relative_to(upload_path.resolve())
        return True
    except (ValueError, OSError):  # fmt: skip
        return False


def _unique_dest(dest: Path) -> Path:
    """Return a unique path by appending _1, _2, etc. if dest already exists."""
    if not dest.exists() and not dest.is_symlink():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    for i in range(1, 100):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    # Fallback: use timestamp
    ts = now_display().strftime("%H%M%S%f")
    return parent / f"{stem}_{ts}{suffix}"


def _generate_photo_filename(file_unique_id: str) -> str:
    """Generate a photo filename: photo_YYYYMMDD_HHMMSS_<8chars>.jpg."""
    timestamp = now_display().strftime("%Y%m%d_%H%M%S")
    short_id = file_unique_id[:8]
    return f"photo_{timestamp}_{short_id}.jpg"


def _resolve_upload_dir(
    user_id: int, thread_id: int | None
) -> tuple[str | None, Path | None, str | None]:
    """Resolve window_id and upload directory for a thread.

    Returns (window_id, upload_path, error_message).
    """
    window_id = thread_router.resolve_window_for_thread(user_id, thread_id)
    if not window_id:
        return None, None, "No session bound to this topic."

    view = view_window(window_id)
    if view is None or not view.cwd:
        return window_id, None, "Session has no working directory."

    upload_path = Path(view.cwd) / _UPLOAD_DIR
    return window_id, upload_path, None


async def _download_and_save(
    message: Message,
    upload_path: Path,
    filename: str,
    file_id: str,
    file_size: int | None,
    size_label: str,
) -> str | None:
    """Download a Telegram file and save it to the upload directory.

    Returns the final filename on success, or None on failure (error already
    replied to the user).
    """
    # Pre-download size check
    if file_size is not None and file_size > _MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        await safe_reply(
            message,
            f"\u274c {size_label} too large ({size_mb:.1f} MB). Maximum {_MAX_FILE_SIZE // (1024 * 1024)} MB.",
        )
        return None

    try:
        upload_path.mkdir(parents=True, exist_ok=True)
        # Reject symlinked upload dir (could redirect uploads outside cwd)
        if upload_path.is_symlink():
            logger.error("Upload dir is a symlink: %s", upload_path)
            await safe_reply(message, "\u274c Upload directory is invalid.")
            return None
        dest = upload_path / filename
        if not _validate_dest_path(dest, upload_path):
            logger.error("Path traversal attempt blocked: %s", filename)
            await safe_reply(message, "\u274c Invalid filename.")
            return None
        dest = _unique_dest(dest)
        filename = dest.name
        file = await message.get_bot().get_file(file_id)
        await file.download_to_drive(str(dest))
        # Post-download size check (file_size can be None from Telegram API)
        actual_size = dest.stat().st_size
        if actual_size > _MAX_FILE_SIZE:
            dest.unlink(missing_ok=True)
            size_mb = actual_size / (1024 * 1024)
            await safe_reply(
                message,
                f"\u274c {size_label} too large ({size_mb:.1f} MB). Maximum {_MAX_FILE_SIZE // (1024 * 1024)} MB.",
            )
            return None
    except (OSError, TelegramError) as e:
        logger.error("Failed to save %s: %s", size_label.lower(), e)
        await safe_reply(message, "\u274c Failed to save file.")
        return None

    return filename


async def _upload_and_notify(
    message: Message,
    user_id: int,
    thread_id: int,
    filename: str,
    file_id: str,
    file_size: int | None,
    size_label: str,
    agent_msg_tpl: str,
    success_emoji: str,
    caption: str,
) -> None:
    """Resolve, save, schedule and dispatch one provider-neutral media task."""
    window_id, upload_path, error = _resolve_upload_dir(user_id, thread_id)
    if error or not window_id or not upload_path:
        await safe_reply(message, f"\u274c {error}")
        return

    await message.chat.send_action(ChatAction.TYPING)

    saved_name = await _download_and_save(
        message, upload_path, filename, file_id, file_size, size_label
    )
    if not saved_name:
        return

    rel_path = f"{_UPLOAD_DIR}/{saved_name}"
    agent_msg = agent_msg_tpl.format(name=saved_name, path=rel_path)
    if caption:
        agent_msg += f"\n\nUser note: {_sanitize_caption(caption)}"

    admission = await admit_request(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        message=message,
        dispatch_text=agent_msg,
    )
    if admission is None:
        return

    chat_id, message_id = message_ids(message)
    inbound_key = inbound_store.make_key(chat_id, thread_id, message_id)
    inbound_store.set_state(inbound_key, "dispatching")
    # Lazy: dispatch confirmation depends on the receipt messaging pipeline.
    from ..dispatch_confirmation import dispatch_confirmation

    await dispatch_confirmation.mark_written(window_id)
    success, err = await send_to_window(window_id, agent_msg)
    if success:
        inbound_store.set_state(inbound_key, "forwarded")
        await ack_reaction(
            PTBTelegramClient(message.get_bot()), message.chat.id, message.message_id
        )
        await safe_reply(
            message,
            t("{emoji} Uploaded `{path}`").format(emoji=success_emoji, path=rel_path),
        )
    else:
        dispatch_confirmation.complete(window_id)
        if admission.continuation:
            inbound_store.set_state(inbound_key, "failed")
        else:
            inbound_store.mark_window_done(window_id, failed=True)
        clear_request_window(window_id)
        if not admission.continuation:
            await task_scheduler.release_window(window_id)
        await safe_reply(
            message,
            t("❌ File saved but failed to notify the agent: {error}").format(
                error=err
            ),
        )


async def _buffer_album_item(
    message: Message,
    user_id: int,
    thread_id: int,
    filename: str,
    file_id: str,
    file_size: int | None,
    size_label: str,
    caption: str,
) -> None:
    """Save one Telegram album member and coalesce it into one CLI request."""
    window_id, upload_path, error = _resolve_upload_dir(user_id, thread_id)
    if error or not window_id or not upload_path:
        await safe_reply(message, f"❌ {error}")
        return
    saved_name = await _download_and_save(
        message, upload_path, filename, file_id, file_size, size_label
    )
    if not saved_name:
        return
    media_group_id = str(getattr(message, "media_group_id", "") or "")
    key = (message.chat.id, thread_id, user_id, media_group_id)
    _album_buffers.setdefault(key, []).append(
        _AlbumItem(
            message=message,
            window_id=window_id,
            user_id=user_id,
            thread_id=thread_id,
            rel_path=f"{_UPLOAD_DIR}/{saved_name}",
            caption=_sanitize_caption(caption),
        )
    )
    previous = _album_flush_tasks.pop(key, None)
    if previous is not None:
        previous.cancel()
    task = asyncio.create_task(_flush_album(key), name=f"media-album:{media_group_id}")
    task.add_done_callback(task_done_callback)
    _album_flush_tasks[key] = task


async def _flush_album(key: tuple[int, int, int, str]) -> None:
    try:
        await asyncio.sleep(config.media_group_coalesce_ms / 1000)
    except asyncio.CancelledError:
        return
    items = _album_buffers.pop(key, [])
    _album_flush_tasks.pop(key, None)
    if not items:
        return
    first = items[0]
    agent_msg = _build_album_prompt(items)
    admission = await admit_request(
        window_id=first.window_id,
        user_id=first.user_id,
        thread_id=first.thread_id,
        message=first.message,
        dispatch_text=agent_msg,
    )
    if admission is None:
        return
    # Lazy: dispatch confirmation depends on the receipt messaging pipeline.
    from ..dispatch_confirmation import dispatch_confirmation

    await dispatch_confirmation.mark_written(first.window_id)
    success, error = await send_to_window(first.window_id, agent_msg)
    if not success and not admission.continuation:
        inbound_store.mark_window_done(first.window_id, failed=True)
    for item in items:
        chat_id, message_id = message_ids(item.message)
        inbound_store.set_state(
            inbound_store.make_key(chat_id, item.thread_id, message_id),
            "forwarded" if success else "failed",
        )
    if not success:
        dispatch_confirmation.complete(first.window_id)
        clear_request_window(first.window_id)
        if not admission.continuation:
            await task_scheduler.release_window(first.window_id)
        await safe_reply(
            first.message,
            t("❌ File saved but failed to notify the agent: {error}").format(
                error=error
            ),
        )
        return
    client = PTBTelegramClient(first.message.get_bot())
    for item in items:
        await ack_reaction(client, item.message.chat.id, item.message.message_id)
    await safe_reply(
        first.message,
        t("📚 Uploaded album: {count} files").format(count=len(items)),
    )


def _build_album_prompt(items: list[_AlbumItem]) -> str:
    """Build one provider-neutral prompt for all files in an album."""
    paths = "\n".join(f"- {item.rel_path}" for item in items)
    captions = [item.caption for item in items if item.caption]
    prompt = f"I've uploaded a media album with {len(items)} files:\n{paths}"
    if captions:
        prompt += "\n\nUser note: " + " ".join(dict.fromkeys(captions))
    return prompt


async def _prepare_media_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Message, int, int, str] | None:
    """Authorize, activate, deduplicate and resolve an isolated media lane."""
    user = update.effective_user
    message = update.message
    if not user or not message:
        return None
    if not config.is_user_allowed(user.id):
        if message.chat.type == "private":
            await safe_reply(message, t("You are not authorized to use this bot."))
        return None

    client = PTBTelegramClient(context.bot)
    activation = evaluate_group_activation(message, client, message.caption or "")
    if not activation.accepted:
        return None

    # Lazy: optional dashboard imports Telegram persistence and this handler.
    from ..operations_dashboard import observe_dashboard_user

    observe_dashboard_user(user)
    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            message,
            t("❌ Please use a named topic. Create a new topic to start a session."),
        )
        return None

    if message.chat.type in ("group", "supergroup"):
        thread_router.set_group_chat_id(user.id, thread_id, message.chat.id)
        lane = await ensure_member_lane(
            user_id=user.id,
            chat_id=message.chat.id,
            thread_id=thread_id,
        )
        if lane.handled and not lane.ready:
            await safe_reply(
                message,
                t("❌ Could not start your isolated workspace lane: {error}").format(
                    error=lane.error
                ),
            )
            return None

    if thread_router.resolve_window_for_thread(user.id, thread_id) is None:
        await safe_reply(
            message,
            t(
                "⚠ Topic not bound — send a text message first to select a workspace, then resend the media."
            ),
        )
        return None

    chat_id, message_id = message_ids(message)
    if not inbound_store.claim_message(
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
    ):
        logger.info(
            "Dropped duplicate Telegram media update",
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user.id,
            message_id=message_id,
        )
        return None
    return message, user.id, thread_id, activation.text


async def handle_photo_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Save a photo and dispatch it through the shared agent task pipeline."""
    prepared = await _prepare_media_request(update, context)
    if prepared is None:
        return
    message, user_id, thread_id, caption = prepared
    if not message.photo:
        return

    photo = message.photo[-1]
    if getattr(message, "media_group_id", None):
        await _buffer_album_item(
            message,
            user_id,
            thread_id,
            _generate_photo_filename(photo.file_unique_id),
            photo.file_id,
            photo.file_size,
            "Photo",
            caption,
        )
        return
    await _upload_and_notify(
        message,
        user_id,
        thread_id,
        _generate_photo_filename(photo.file_unique_id),
        photo.file_id,
        photo.file_size,
        "Photo",
        "I've uploaded an image to {path} — please take a look.",
        "\U0001f4f7",
        caption,
    )


async def handle_document_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Save a document and dispatch it through the shared agent task pipeline."""
    prepared = await _prepare_media_request(update, context)
    if prepared is None:
        return
    message, user_id, thread_id, caption = prepared
    if not message.document:
        return

    doc = message.document
    if getattr(message, "media_group_id", None):
        await _buffer_album_item(
            message,
            user_id,
            thread_id,
            _sanitize_filename(doc.file_name or "document"),
            doc.file_id,
            doc.file_size,
            "File",
            caption,
        )
        return
    await _upload_and_notify(
        message,
        user_id,
        thread_id,
        _sanitize_filename(doc.file_name or "document"),
        doc.file_id,
        doc.file_size,
        "File",
        "I've uploaded {name} to {path}",
        "\U0001f4ce",
        caption,
    )
