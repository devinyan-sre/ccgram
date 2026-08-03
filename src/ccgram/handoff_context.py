"""Build an optional cross-provider context handoff prompt."""

from __future__ import annotations

import asyncio

import structlog

from .metrics import LLM_REQUESTS
from . import session_query, window_query

logger = structlog.get_logger()

_MAX_SOURCE_CHARS = 8_000
_MAX_MESSAGE_CHARS = 1_200
_MAX_SUMMARY_CHARS = 4_000
_MAX_MESSAGES = 16
_MAX_TRANSCRIPT_TAIL_BYTES = 512 * 1024
_HANDOFF_SYSTEM = """\
Summarize an in-progress software-engineering conversation for another coding
agent. Preserve the goal, completed changes, commands/tests run, failures,
important file paths, and concrete next steps. Do not invent details. Return a
compact operational handoff, without greetings or markdown fences."""


def _local_digest(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages[-_MAX_MESSAGES:]:
        if message.get("content_type") != "text":
            continue
        role = message.get("role")
        text = str(message.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        label = "User" if role == "user" else "Assistant"
        parts.append(f"{label}: {text[:_MAX_MESSAGE_CHARS]}")
    digest = "\n\n".join(parts)
    return digest[-_MAX_SOURCE_CHARS:]


def transcript_tail_offset(
    window_id: str, max_bytes: int = _MAX_TRANSCRIPT_TAIL_BYTES
) -> int:
    """Return a bounded transcript start offset for recent-message reads."""
    view = window_query.view_window(window_id)
    if view is None or not view.transcript_path:
        return 0
    try:
        return max(0, view.transcript_path.stat().st_size - max_bytes)
    except OSError:
        return 0


async def generate_handoff_context(window_id: str) -> str:
    """Return a bounded continuation prompt for a replacement provider."""
    messages, _ = await session_query.get_recent_messages(
        window_id, start_byte=transcript_tail_offset(window_id)
    )
    source = _local_digest(messages)
    if not source:
        return ""

    summary = ""
    try:
        # Lazy: deployments without an LLM keep a deterministic local fallback.
        from .llm import get_text_completer

        completer = get_text_completer()
        if completer is not None:
            summary = await asyncio.wait_for(
                completer.complete(_HANDOFF_SYSTEM, source), timeout=15.0
            )
            LLM_REQUESTS.inc(kind="handoff", provider="configured", outcome="ok")
    except Exception:  # noqa: BLE001 - context transfer must never block migration
        LLM_REQUESTS.inc(kind="handoff", provider="configured", outcome="error")
        logger.warning("Provider handoff summary failed; using local digest")

    body = (summary or source).strip()[:_MAX_SUMMARY_CHARS]
    return (
        "Continue this task using the handoff below. Verify the current workspace "
        "state before changing anything.\n\n"
        f"{body}"
    )
