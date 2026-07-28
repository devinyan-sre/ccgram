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
from .digest_text import DigestStats, analyze

if TYPE_CHECKING:
    from telegram.ext import Application, ContextTypes

    from ..telegram_client import TelegramClient

logger = structlog.get_logger()

# Read at most this much of a transcript's tail when counting recent turns.
_TAIL_BYTES = 2 * 1024 * 1024
_DAY_SECONDS = 24 * 3600


def _entry_ts(entry: dict[str, object]) -> float | None:
    """Epoch seconds for a transcript entry, or None when unusable."""
    ts_raw = entry.get("timestamp")
    if not isinstance(ts_raw, str):
        return None
    try:
        return _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def scan_transcript(
    transcript_path: Path, since_ts: float
) -> tuple[DigestStats, float | None]:
    """Analyse the digest window of a transcript. Returns (stats, last_ts).

    Reads only the file tail (``_TAIL_BYTES``) so huge transcripts stay
    cheap; a partial first line after the seek is skipped safely by the
    JSON decode guard. Parsing rules live in ``digest_text`` — this function
    only does I/O and time filtering.
    """
    recent: list[dict[str, object]] = []
    last_ts: float | None = None
    try:
        size = transcript_path.stat().st_size
        with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # skip the partial line
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                ts = _entry_ts(entry)
                if ts is None:
                    continue
                if last_ts is None or ts > last_ts:
                    last_ts = ts
                if ts >= since_ts:
                    recent.append(entry)
    except OSError:
        return DigestStats(), None
    return analyze(recent), last_ts


def _idle_suffix(last_ts: float | None, now_ts: float) -> str:
    """Trailing status marker: active, or how long the topic has been quiet.

    Idle time is the digest's "should I look here?" signal — a topic nobody
    has touched for two days reads very differently from one that stopped an
    hour ago, even when both show zero activity.
    """
    if last_ts is None:
        return ""
    hours = (now_ts - last_ts) / 3600
    if hours < 1:
        return " 🟢"
    if hours < 24:  # noqa: PLR2004 — hours in a day
        return " 💤 " + t("idle {hours}h").format(hours=int(hours))
    return " 💤 " + t("idle {days}d").format(days=int(hours // 24))


def _format_stats(stats: DigestStats) -> list[str]:
    """The indented detail lines under one topic's headline."""
    lines: list[str] = []
    counts = t("{users} prompts / {replies} replies").format(
        users=stats.prompts, replies=stats.replies
    )
    if stats.tools:
        tools = " · ".join(f"{name} {count}" for name, count in stats.tools[:3])
        counts = f"{counts} · {tools}"
    lines.append(f"    {counts}")
    if stats.keywords:
        lines.append("    🔑 " + " · ".join(stats.keywords))
    if stats.errors:
        lines.append("    ⚠ " + t("{count} tool errors").format(count=stats.errors))
    return lines


def _digest_lines(window_id: str, since_ts: float, now_ts: float) -> list[str]:
    """Digest block for one bound window (blocking)."""
    view = view_window(window_id)
    name = thread_router.get_display_name(window_id) or window_id
    provider = (view.provider_name if view else "") or "?"
    head = f"• {name} ({provider})"

    if view is None or not view.transcript_path:
        return [f"{head} — " + t("no transcript")]

    stats, last_ts = scan_transcript(Path(view.transcript_path), since_ts)
    if stats.is_empty:
        return [f"{head}{_idle_suffix(last_ts, now_ts)} — " + t("no activity in 24h")]
    return [f"{head}{_idle_suffix(last_ts, now_ts)}", *_format_stats(stats)]


async def build_digest_for_user(_user_id: int, window_ids: list[str]) -> str:
    """Build the digest text for one user's bound windows."""
    now_ts = _dt.datetime.now().timestamp()
    since_ts = now_ts - _DAY_SECONDS
    lines = [
        t("☀️ Daily digest — last 24h"),
        "",
    ]
    for window_id in window_ids:
        block = await asyncio.to_thread(_digest_lines, window_id, since_ts, now_ts)
        lines.extend(block)
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
