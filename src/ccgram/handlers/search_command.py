"""/search command — full-text search across Claude session transcripts.

Scans ``~/.claude/projects/**/*.jsonl`` (newest files first) for a keyword
in user/assistant message text and replies with snippet matches. Global —
works in any chat, no topic binding required. Bounded by a file cap, a
hit cap, and a wall-clock budget so huge histories can't wedge the bot.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..i18n import t

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_MAX_HITS = 10
_MAX_FILES = 300
_TIME_BUDGET_SECONDS = 8.0
_SNIPPET_CONTEXT = 60
_MAX_QUERY_LEN = 200


@dataclass(frozen=True)
class SearchHit:
    """One transcript match."""

    project: str
    session_id: str
    role: str  # "user" | "assistant"
    timestamp: str  # "YYYY-MM-DD HH:MM" or ""
    snippet: str


def _extract_text(entry: dict) -> str:
    """Return the human-readable text of a user/assistant transcript entry."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _make_snippet(text: str, query: str) -> str:
    """Return the match with ±context chars, newlines collapsed."""
    lowered = text.lower()
    idx = lowered.find(query.lower())
    if idx < 0:
        return ""
    start = max(0, idx - _SNIPPET_CONTEXT)
    end = min(len(text), idx + len(query) + _SNIPPET_CONTEXT)
    snippet = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _project_label(entry: dict, file_path: Path) -> str:
    """Best project label: the entry's cwd, else the encoded dir name."""
    cwd = entry.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return file_path.parent.name


# "YYYY-MM-DDTHH:MM" prefix of an ISO timestamp — minute precision.
_TS_MINUTES_LEN = 16


def _format_timestamp(entry: dict) -> str:
    ts = entry.get("timestamp")
    if isinstance(ts, str) and len(ts) >= _TS_MINUTES_LEN:
        return ts[:_TS_MINUTES_LEN].replace("T", " ")
    return ""


def search_transcripts(projects_path: Path, query: str) -> tuple[list[SearchHit], bool]:
    """Search transcripts for *query* (blocking; call via ``asyncio.to_thread``).

    Returns ``(hits, truncated)``: newest transcript files are scanned first,
    and the scan stops at the hit cap, the file cap, or the time budget —
    ``truncated`` reports whether any of those cut the scan short.
    """
    deadline = time.monotonic() + _TIME_BUDGET_SECONDS
    query_lower = query.lower()

    try:
        files = [p for p in projects_path.glob("*/*.jsonl") if p.is_file()]
    except OSError:
        return [], False

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=_mtime, reverse=True)
    truncated = len(files) > _MAX_FILES
    files = files[:_MAX_FILES]

    hits: list[SearchHit] = []
    for file_path in files:
        if len(hits) >= _MAX_HITS or time.monotonic() > deadline:
            truncated = True
            break
        file_hits = _scan_file(file_path, query, query_lower)
        # Newest matches of each file first (files are already newest-first).
        file_hits.reverse()
        hits.extend(file_hits)

    if len(hits) > _MAX_HITS:
        truncated = True
        hits = hits[:_MAX_HITS]
    return hits, truncated


def _scan_file(file_path: Path, query: str, query_lower: str) -> list[SearchHit]:
    """Collect all query matches from one transcript file."""
    file_hits: list[SearchHit] = []
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                # Cheap raw pre-filter before the JSON parse.
                if query_lower not in line.lower():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") not in ("user", "assistant"):
                    continue
                if entry.get("isMeta") or entry.get("isSidechain"):
                    continue
                snippet = _make_snippet(_extract_text(entry), query)
                if not snippet:
                    continue  # matched tool internals / metadata, not text
                file_hits.append(
                    SearchHit(
                        project=_project_label(entry, file_path),
                        session_id=str(entry.get("sessionId", file_path.stem)),
                        role=str(entry.get("type")),
                        timestamp=_format_timestamp(entry),
                        snippet=snippet,
                    )
                )
    except OSError:
        return file_hits
    return file_hits


def format_results(query: str, hits: list[SearchHit], truncated: bool) -> str:
    """Render search hits as one markdown message."""
    if not hits:
        return t("🔍 No matches for “{query}”.").format(query=query)

    lines = [
        t("🔍 {count} match(es) for “{query}”:").format(count=len(hits), query=query)
    ]
    for i, hit in enumerate(hits, 1):
        role_icon = "👤" if hit.role == "user" else "🤖"
        meta = " · ".join(
            part
            for part in (
                hit.project,
                hit.timestamp,
                f"{role_icon} {hit.session_id[:8]}",
            )
            if part
        )
        lines.append(f"\n{i}. 📂 {meta}\n> {hit.snippet}")
    if truncated:
        lines.append("\n" + t("_(results truncated — refine your query)_"))
    return "\n".join(lines)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /search — keyword search across session transcripts."""
    # Lazy: config singleton resolved at call time so tests can swap it
    from ..config import config

    # Lazy: messaging_pipeline ↔ handler cycle through status_bubble
    from .messaging_pipeline.message_sender import safe_reply

    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    query = " ".join(context.args or []).strip()
    if not query:
        await safe_reply(
            update.message,
            t("Usage: /search <keyword> — search across session transcripts"),
        )
        return
    query = query[:_MAX_QUERY_LEN]

    # Lazy: asyncio only needed for the to_thread call in this handler
    import asyncio

    hits, truncated = await asyncio.to_thread(
        search_transcripts, config.claude_projects_path, query
    )
    await safe_reply(update.message, format_results(query, hits, truncated))
