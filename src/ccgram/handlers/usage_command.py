"""/usage command — token usage for the topic's current session.

Parses the bound window's transcript JSONL and sums the per-turn ``usage``
blocks Claude Code records on assistant entries (Codex/Gemini transcripts
without usage data degrade to a friendly notice).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..i18n import t
from ..thread_router import thread_router
from ..window_query import view_window

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()


@dataclass
class UsageTotals:
    """Aggregated token usage across a transcript."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    assistant_turns: int = 0
    user_turns: int = 0
    models: set[str] = field(default_factory=set)

    @property
    def has_data(self) -> bool:
        return self.assistant_turns > 0 and bool(
            self.input_tokens or self.output_tokens or self.cache_read_tokens
        )


def collect_usage(transcript_path: Path) -> UsageTotals | None:
    """Sum usage blocks from a JSONL transcript (blocking; run in a thread).

    Returns None when the file is unreadable.
    """
    totals = UsageTotals()
    try:
        with transcript_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _accumulate(totals, entry)
    except OSError:
        return None
    return totals


def _accumulate(totals: UsageTotals, entry: dict) -> None:
    """Fold one transcript entry into the running totals."""
    entry_type = entry.get("type")
    if entry_type == "user":
        totals.user_turns += 1
        return
    if entry_type != "assistant":
        return
    totals.assistant_turns += 1
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    model = message.get("model")
    if isinstance(model, str) and model:
        totals.models.add(model)
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    totals.input_tokens += int(usage.get("input_tokens") or 0)
    totals.output_tokens += int(usage.get("output_tokens") or 0)
    totals.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
    totals.cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)


def format_usage(totals: UsageTotals, session_id: str) -> str:
    """Render totals as a compact Telegram message."""

    million = 1_000_000
    thousand = 1_000

    def fmt(n: int) -> str:
        if n >= million:
            return f"{n / million:.1f}M"
        if n >= thousand:
            return f"{n / thousand:.1f}K"
        return str(n)

    lines = [t("📊 Session token usage") + f" (`{session_id[:8]}`)", ""]
    if totals.models:
        lines.append(
            t("Model: {models}").format(models=", ".join(sorted(totals.models)))
        )
    lines.append(
        t("Turns: {user} user / {assistant} assistant").format(
            user=totals.user_turns, assistant=totals.assistant_turns
        )
    )
    lines.append("")
    lines.append(t("Input tokens: {n}").format(n=fmt(totals.input_tokens)))
    lines.append(t("Output tokens: {n}").format(n=fmt(totals.output_tokens)))
    lines.append(t("Cache read: {n}").format(n=fmt(totals.cache_read_tokens)))
    lines.append(t("Cache write: {n}").format(n=fmt(totals.cache_creation_tokens)))
    total = (
        totals.input_tokens
        + totals.output_tokens
        + totals.cache_read_tokens
        + totals.cache_creation_tokens
    )
    lines.append("")
    lines.append(t("Total: {n}").format(n=fmt(total)))
    return "\n".join(lines)


async def usage_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage — token usage for this topic's current session."""
    # Lazy: config singleton resolved at call time so tests can swap it
    from ..config import config

    # Lazy: messaging_pipeline ↔ handler cycle through status_bubble
    from .messaging_pipeline.message_sender import safe_reply

    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    # Lazy: callback_helpers only used when we have a real update
    from .callback_helpers import get_thread_id

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, t("❌ Use this command inside a topic."))
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, t("❌ This topic is not bound to any session.")
        )
        return

    view = view_window(window_id)
    if view is None or not view.transcript_path:
        await safe_reply(
            update.message,
            t("❌ No transcript for this session yet (usage data unavailable)."),
        )
        return

    # Lazy: asyncio only needed for the thread offload below
    import asyncio

    totals = await asyncio.to_thread(collect_usage, Path(view.transcript_path))
    if totals is None:
        await safe_reply(update.message, t("❌ Could not read the transcript file."))
        return
    if not totals.has_data:
        await safe_reply(
            update.message,
            t("❌ This provider's transcript has no token usage data."),
        )
        return

    await safe_reply(update.message, format_usage(totals, view.session_id))
