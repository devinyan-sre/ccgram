"""Daily activity digest — one morning summary per user.

At the configured local time (``CCGRAM_DAILY_DIGEST="HH:MM"``), posts a
per-topic activity summary to each user's group (General topic): session
name, provider, and message counts over the past 24 hours, read from the
tail of each session's transcript.

Key functions: setup_daily_digest_job() (bootstrap), build_digest_for_user().
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..i18n import t
from ..thread_router import thread_router
from ..window_query import view_window

if TYPE_CHECKING:
    from telegram.ext import Application, ContextTypes

    from ..telegram_client import TelegramClient

logger = structlog.get_logger()

# Read at most this much of a transcript's tail when counting recent turns.
_TAIL_BYTES = 2 * 1024 * 1024
_DAY_SECONDS = 24 * 3600


def count_recent_turns(transcript_path: Path, since_ts: float) -> tuple[int, int]:
    """Count (user, assistant) turns newer than *since_ts* (blocking).

    Reads only the file tail (``_TAIL_BYTES``) so huge transcripts stay
    cheap; a partial first line after the seek is skipped safely by the
    JSON decode guard.
    """
    users = assistants = 0
    try:
        size = transcript_path.stat().st_size
        with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # skip the partial line
            for line in fh:
                entry_type = _recent_entry_type(line, since_ts)
                if entry_type == "user":
                    users += 1
                elif entry_type == "assistant":
                    assistants += 1
    except OSError:
        return 0, 0
    return users, assistants


def _recent_entry_type(line: str, since_ts: float) -> str | None:
    """Return the entry type for a JSONL line newer than *since_ts*, else None."""
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts_raw = entry.get("timestamp")
    if not isinstance(ts_raw, str):
        return None
    try:
        ts = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    if ts < since_ts:
        return None
    entry_type = entry.get("type")
    return entry_type if isinstance(entry_type, str) else None


def _digest_line(window_id: str, since_ts: float) -> str | None:
    """One digest line for a bound window, or None to skip (blocking)."""
    view = view_window(window_id)
    name = thread_router.get_display_name(window_id) or window_id
    provider = (view.provider_name if view else "") or "?"

    if view is None or not view.transcript_path:
        return f"• {name} ({provider}) — " + t("no transcript")

    users, assistants = count_recent_turns(Path(view.transcript_path), since_ts)
    if users == 0 and assistants == 0:
        return f"• {name} ({provider}) — " + t("no activity in 24h")
    return f"• {name} ({provider}) — " + t(
        "{users} prompts / {replies} replies"
    ).format(users=users, replies=assistants)


async def build_digest_for_user(_user_id: int, window_ids: list[str]) -> str:
    """Build the digest text for one user's bound windows."""
    since_ts = _dt.datetime.now().timestamp() - _DAY_SECONDS
    lines = [
        t("☀️ Daily digest — last 24h"),
        "",
    ]
    for window_id in window_ids:
        line = await asyncio.to_thread(_digest_line, window_id, since_ts)
        if line:
            lines.append(line)
    return "\n".join(lines)


async def send_daily_digest(client: TelegramClient) -> None:
    """Send the digest to every user with bound topics (General topic)."""
    # Lazy: messaging_pipeline ↔ handler cycle through status_bubble
    from .messaging_pipeline.message_sender import safe_send

    per_user: dict[int, list[str]] = {}
    for user_id, _thread_id, window_id in thread_router.iter_thread_bindings():
        per_user.setdefault(user_id, []).append(window_id)

    for user_id, window_ids in per_user.items():
        try:
            chat_id = thread_router.resolve_chat_id(user_id)
        except KeyError, RuntimeError:
            continue
        text = await build_digest_for_user(user_id, window_ids)
        await safe_send(client, chat_id, text)


def setup_daily_digest_job(application: Application) -> None:
    """Register the daily digest job when CCGRAM_DAILY_DIGEST is set."""
    # Lazy: config singleton resolved at wiring time so tests can swap it
    from ..config import config

    spec = getattr(config, "daily_digest_time", "")
    if not spec:
        return
    try:
        at = _dt.time.fromisoformat(spec)
    except ValueError:
        logger.warning("Invalid CCGRAM_DAILY_DIGEST %r (expected HH:MM)", spec)
        return

    async def _run(context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.bot:
            return
        # Lazy: PTBTelegramClient only needed with a live bot
        from ..telegram_client import PTBTelegramClient

        try:
            await send_daily_digest(PTBTelegramClient(context.bot))
        except Exception:  # noqa: BLE001 — digest failure must not kill the job queue
            logger.warning("Daily digest failed", exc_info=True)

    jq = getattr(application, "job_queue", None)
    if jq is not None:
        jq.run_daily(_run, time=at)
        logger.info("Daily digest scheduled at %s", spec)
