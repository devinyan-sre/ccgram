"""/diff command — review uncommitted git changes from Telegram.

Sends the bound window's working-tree diff (vs HEAD) to the topic:
a short status summary plus the full diff, overflowing to a ``.diff``
document when it exceeds Telegram's message limit. Optional path
arguments narrow the diff (``/diff src/foo.py``).
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING

import structlog

from ..i18n import t
from ..multiplexer import multiplexer as tmux_manager
from ..telegram_client import TelegramClient
from ..thread_router import thread_router

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_GIT_TIMEOUT = 15
_INLINE_LIMIT = 3500  # leave room for the code fence + entity overhead


def _run_git(cwd: str, *args: str) -> tuple[int, str, str]:
    """Run a git command in *cwd* (blocking; call via ``asyncio.to_thread``)."""
    try:
        proc = subprocess.run(  # fixed binary, list argv — no shell
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def build_diff_report(cwd: str, paths: list[str]) -> tuple[str, str] | str:
    """Collect status + diff for *cwd* (blocking; run in a thread).

    Returns ``(summary, diff_text)`` on success or an error/notice string.
    ``paths`` narrows the diff; entries starting with ``-`` are rejected
    upstream so they cannot be interpreted as git flags.
    """
    rc, status_out, status_err = _run_git(cwd, "status", "--short")
    if rc != 0:
        if "not a git repository" in status_err.lower():
            return t("❌ Not a git repository: {cwd}").format(cwd=cwd)
        return t("❌ git failed: {error}").format(error=status_err.strip()[:200])

    scope = ["--", *paths] if paths else []
    _, stat_out, _ = _run_git(cwd, "diff", "HEAD", "--stat", *scope)
    _, diff_out, _ = _run_git(cwd, "diff", "HEAD", *scope)

    if not status_out.strip() and not diff_out.strip():
        return t("✅ Working tree clean — no uncommitted changes.")

    summary_parts = []
    if status_out.strip():
        summary_parts.append(
            t("📋 Status:") + "\n```\n" + status_out.strip()[:800] + "\n```"
        )
    if stat_out.strip():
        summary_parts.append("```\n" + stat_out.strip()[:1200] + "\n```")
    summary = "\n".join(summary_parts)

    return summary, diff_out


async def send_diff(
    client: TelegramClient,
    chat_id: int,
    thread_id: int | None,
    window_id: str,
    cwd: str,
    paths: list[str],
) -> None:
    """Send the diff report for *cwd* to a topic (document on overflow)."""
    # Lazy: messaging_pipeline ↔ handler cycle through status_bubble
    from .messaging_pipeline.message_sender import safe_send

    result = await asyncio.to_thread(build_diff_report, cwd, paths)
    if isinstance(result, str):
        await safe_send(client, chat_id, result, message_thread_id=thread_id)
        return

    summary, diff_text = result
    diff_text = diff_text.strip()

    if not diff_text:
        # Only untracked files — the status summary is the whole story.
        await safe_send(client, chat_id, summary, message_thread_id=thread_id)
        return

    if len(summary) + len(diff_text) <= _INLINE_LIMIT:
        message = summary + "\n```diff\n" + diff_text + "\n```"
        await safe_send(client, chat_id, message, message_thread_id=thread_id)
        return

    # Overflow: summary inline, full diff as a document.
    await safe_send(client, chat_id, summary, message_thread_id=thread_id)

    # Lazy: only used in this overflow branch
    import tempfile

    # Lazy: only used in this overflow branch
    from pathlib import Path

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".diff", mode="w", encoding="utf-8"
        ) as fh:
            fh.write(diff_text)
            tmp_path = fh.name

        window_label = (
            "".join(c if c.isalnum() or c in "_-" else "-" for c in window_id).strip(
                "-"
            )
            or "window"
        )
        await client.send_document(
            chat_id=chat_id,
            document=Path(tmp_path),
            filename=f"diff-{window_label}.diff",
            **({"message_thread_id": thread_id} if thread_id is not None else {}),
        )
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


async def diff_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /diff — send the bound window's uncommitted changes."""
    # Lazy: config singleton resolved at call time so tests can swap it
    from ..config import config

    # Lazy: messaging_pipeline ↔ handler cycle through status_bubble
    from .messaging_pipeline.message_sender import safe_reply

    # Lazy: PTBTelegramClient only needed when we have a real bot context
    from ..telegram_client import PTBTelegramClient

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

    w = await tmux_manager.find_window_by_id(window_id)
    if not w or not w.cwd:
        await safe_reply(update.message, t("❌ Window no longer exists."))
        return

    paths = [a for a in (context.args or []) if not a.startswith("-")]

    chat_id = thread_router.resolve_chat_id(user.id, thread_id)
    client = PTBTelegramClient(update.message.get_bot())
    await send_diff(client, chat_id, thread_id, window_id, w.cwd, paths)
