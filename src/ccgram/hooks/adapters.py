"""Provider adapters for command hook stdin payloads.

The adapters validate common fields, map native event names to ccgram's canonical
lifecycle events, and retain only metadata safe enough for ``events.jsonl``.
"""

from __future__ import annotations

from collections.abc import Callable

import os
from pathlib import Path
from typing import cast

from ccgram.providers.base import UUID_RE

from .model import HookAdapter, JsonValue, NormalizedHookEvent, ProviderName

_SAFE_PROVIDERS: tuple[ProviderName, ...] = ("claude", "pi", "codex", "gemini")

# Event names emitted only by Gemini — used by detect_provider_from_payload
# to distinguish Gemini payloads when transcript path is absent. SessionStart,
# SessionEnd, and Notification are shared across providers so they don't help.
_GEMINI_ONLY_EVENT_TYPES: tuple[str, ...] = (
    "AfterAgent",
    "BeforeAgent",
    "BeforeTool",
    "AfterTool",
    "PreCompress",
)


def _str_field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _int_field(payload: dict[str, object], key: str, default: int = 0) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) else default


def _bool_field(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    return value if isinstance(value, bool) else False


def _path_or_none(value: str) -> Path | None:
    if not value:
        return None
    return Path(value) if os.path.isabs(value) else None


def _safe_details_tool_name(payload: dict[str, object]) -> str:
    details = payload.get("details")
    if not isinstance(details, dict):
        return ""
    tool_name = details.get("tool_name") or details.get("toolName")
    return tool_name if isinstance(tool_name, str) else ""


def _event(
    *,
    provider_name: ProviderName,
    native_event_name: str,
    canonical_event_name: str,
    session_id: str,
    cwd: str,
    transcript_path: str,
    data: dict[str, JsonValue] | None = None,
) -> NormalizedHookEvent | None:
    if not session_id or not native_event_name:
        return None
    cwd_path = _path_or_none(cwd)
    if cwd and cwd_path is None:
        return None
    if canonical_event_name == "SessionStart" and cwd_path is None:
        return None
    safe_data: dict[str, JsonValue] = {
        "provider_name": provider_name,
        "native_event_name": native_event_name,
    }
    if data:
        safe_data.update(data)
    return NormalizedHookEvent(
        provider_name=provider_name,
        native_event_name=native_event_name,
        canonical_event_name=canonical_event_name,
        session_id=session_id,
        cwd=cwd_path,
        transcript_path=_path_or_none(transcript_path),
        data=safe_data,
    )


class ClaudeHookAdapter:
    """Normalize Claude Code hook payloads."""

    provider_name: ProviderName = "claude"
    event_types: tuple[str, ...] = (
        "SessionStart",
        "Notification",
        "Stop",
        "StopFailure",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
        "TeammateIdle",
        "TaskCompleted",
        "PreCompact",
    )
    # Claude installs all of its event types — the install path uses
    # ~/.claude/settings.json schema rather than the JSON-hook installer,
    # but installable_events lets call sites read every adapter uniformly.
    installable_events: tuple[str, ...] = event_types

    def normalize(self, payload: dict[str, object]) -> NormalizedHookEvent | None:
        event_name = _str_field(payload, "hook_event_name")
        session_id = _str_field(payload, "session_id")
        if event_name not in self.event_types or not UUID_RE.match(session_id):
            return None
        data = _extract_claude_data(event_name, payload)
        return _event(
            provider_name=self.provider_name,
            native_event_name=event_name,
            canonical_event_name=event_name,
            session_id=session_id,
            cwd=_str_field(payload, "cwd"),
            transcript_path=_str_field(payload, "transcript_path"),
            data=data,
        )


class PiHookAdapter:
    """Normalize Pi hook-runner payloads."""

    provider_name: ProviderName = "pi"
    event_types: tuple[str, ...] = (
        "SessionStart",
        "Stop",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
        "Notification",
        "PreCompact",
        "PostCompact",
    )
    # Pi hooks are installed by cc-thingz hook-runner, not ccgram.
    installable_events: tuple[str, ...] = ()

    def normalize(self, payload: dict[str, object]) -> NormalizedHookEvent | None:
        event_name = _str_field(payload, "hook_event_name")
        if event_name not in self.event_types:
            return None
        session_id = _str_field(payload, "session_id")
        if not UUID_RE.match(session_id):
            return None
        canonical = event_name
        data: dict[str, JsonValue] = {}
        if event_name == "SessionEnd":
            data["reason"] = _str_field(payload, "reason") or _str_field(
                payload, "end_reason"
            )
        elif event_name == "Notification":
            data["message"] = _str_field(payload, "message")
            data["notification_type"] = _str_field(payload, "notification_type")
            data["tool_name"] = _str_field(payload, "tool_name")
        elif event_name in {"SubagentStart", "SubagentStop"}:
            data["subagent_id"] = _str_field(payload, "subagent_id")
            data["name"] = _str_field(payload, "name") or "Pi agent"
            data["description"] = _str_field(payload, "description")
        return _event(
            provider_name=self.provider_name,
            native_event_name=event_name,
            canonical_event_name=canonical,
            session_id=session_id,
            cwd=_str_field(payload, "cwd"),
            transcript_path=_str_field(payload, "transcript_path"),
            data=data,
        )


class CodexHookAdapter:
    """Normalize Codex hook payloads."""

    provider_name: ProviderName = "codex"
    event_types: tuple[str, ...] = (
        "SessionStart",
        "Stop",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "UserPromptSubmit",
        "PreCompact",
        "PostCompact",
    )
    # Only the lifecycle signals ccgram acts on — the rest are accepted by
    # normalize() in case Codex starts emitting them with useful data.
    installable_events: tuple[str, ...] = ("SessionStart", "Stop")

    def normalize(self, payload: dict[str, object]) -> NormalizedHookEvent | None:
        event_name = _str_field(payload, "hook_event_name")
        if event_name not in self.event_types:
            return None
        session_id = _str_field(payload, "session_id")
        if not UUID_RE.match(session_id):
            return None
        canonical = event_name
        data: dict[str, JsonValue] = {}
        if event_name == "Stop":
            data["stop_hook_active"] = _bool_field(payload, "stop_hook_active")
            data["stop_reason"] = _str_field(payload, "stopReason")
        elif event_name in {"PreToolUse", "PostToolUse", "PermissionRequest"}:
            data["tool_name"] = _str_field(payload, "tool_name")
        elif event_name == "SessionStart":
            data["source"] = _str_field(payload, "source")
        elif event_name in {"PreCompact", "PostCompact"}:
            data["trigger"] = _str_field(payload, "trigger")
        return _event(
            provider_name=self.provider_name,
            native_event_name=event_name,
            canonical_event_name=canonical,
            session_id=session_id,
            cwd=_str_field(payload, "cwd"),
            transcript_path=_str_field(payload, "transcript_path"),
            data=data,
        )


class GeminiHookAdapter:
    """Normalize Gemini CLI hook payloads."""

    provider_name: ProviderName = "gemini"
    event_types: tuple[str, ...] = (
        "SessionStart",
        "SessionEnd",
        "Notification",
        "AfterAgent",
        "BeforeAgent",
        "BeforeTool",
        "AfterTool",
        "PreCompress",
    )
    # AfterAgent maps to canonical Stop; the rest line up directly.
    installable_events: tuple[str, ...] = (
        "SessionStart",
        "AfterAgent",
        "SessionEnd",
        "Notification",
    )

    def normalize(self, payload: dict[str, object]) -> NormalizedHookEvent | None:
        # Gemini session IDs are not UUIDs in current builds (free-form CLI
        # tokens). Skipping UUID validation here is intentional; the empty-
        # session_id and absolute-cwd checks in _event still apply.
        event_name = _str_field(payload, "hook_event_name")
        if event_name not in self.event_types:
            return None
        canonical = "Stop" if event_name == "AfterAgent" else event_name
        data: dict[str, JsonValue] = {}
        if event_name == "SessionEnd":
            data["reason"] = _str_field(payload, "reason")
        elif event_name == "Notification":
            data["message"] = _str_field(payload, "message")
            data["notification_type"] = _str_field(payload, "notification_type")
            data["tool_name"] = _safe_details_tool_name(payload)
        elif event_name == "AfterAgent":
            data["stop_hook_active"] = _bool_field(payload, "stop_hook_active")
        elif event_name in {"BeforeTool", "AfterTool"}:
            data["tool_name"] = _str_field(payload, "tool_name")
        elif event_name == "SessionStart":
            data["source"] = _str_field(payload, "source")
        elif event_name == "PreCompress":
            data["trigger"] = _str_field(payload, "trigger")
        return _event(
            provider_name=self.provider_name,
            native_event_name=event_name,
            canonical_event_name=canonical,
            session_id=_str_field(payload, "session_id"),
            cwd=_str_field(payload, "cwd"),
            transcript_path=_str_field(payload, "transcript_path"),
            data=data,
        )


# Per-event field extraction. A table of small functions rather than a chain
# of `if event_name == ...: return {...}` — one entry per event keeps adding an
# event a one-line change instead of another return statement.
_CLAUDE_EXTRACTORS: dict[str, Callable[[dict[str, object]], dict[str, JsonValue]]] = {
    "Notification": lambda p: {
        "tool_name": _str_field(p, "tool_name"),
        "message": _str_field(p, "message"),
    },
    "Stop": lambda p: {
        "stop_reason": _str_field(p, "stop_reason"),
        "num_turns": _int_field(p, "num_turns"),
    },
    "StopFailure": lambda p: {
        "error": _str_field(p, "error"),
        "error_details": _str_field(p, "error_details"),
    },
    "SessionEnd": lambda p: {"reason": _str_field(p, "reason")},
    "SubagentStart": lambda p: {
        "subagent_id": _str_field(p, "subagent_id"),
        "description": _str_field(p, "description"),
        "name": _str_field(p, "name"),
    },
    "TeammateIdle": lambda p: {
        "teammate_name": _str_field(p, "teammate_name"),
        "team_name": _str_field(p, "team_name"),
    },
    # "manual" (user ran /compact) vs "auto" (context filled up). The
    # distinction is the whole point of announcing it: an auto compaction is
    # the one nobody asked for and nobody would otherwise notice.
    "PreCompact": lambda p: {"trigger": _str_field(p, "trigger")},
    "TaskCompleted": lambda p: {
        "task_id": _str_field(p, "task_id"),
        "task_subject": _str_field(p, "task_subject"),
        "task_description": _str_field(p, "task_description"),
        "teammate_name": _str_field(p, "teammate_name"),
        "team_name": _str_field(p, "team_name"),
    },
}
_CLAUDE_EXTRACTORS["SubagentStop"] = _CLAUDE_EXTRACTORS["SubagentStart"]


def _extract_claude_data(
    event_name: str, payload: dict[str, object]
) -> dict[str, JsonValue]:
    extractor = _CLAUDE_EXTRACTORS.get(event_name)
    return extractor(payload) if extractor is not None else {}


_ADAPTERS: dict[ProviderName, HookAdapter] = {
    "claude": ClaudeHookAdapter(),
    "pi": PiHookAdapter(),
    "codex": CodexHookAdapter(),
    "gemini": GeminiHookAdapter(),
}


def get_hook_adapter(provider_name: str) -> HookAdapter | None:
    """Return the hook adapter for a provider name."""
    if provider_name not in _SAFE_PROVIDERS:
        return None
    return _ADAPTERS[cast(ProviderName, provider_name)]


def detect_provider_from_payload(payload: dict[str, object]) -> ProviderName | None:
    """Best-effort provider detection when installed hook lacks --provider."""
    explicit = _str_field(payload, "provider_name")
    transcript_path = _str_field(payload, "transcript_path")
    event_name = _str_field(payload, "hook_event_name")
    session_id = _str_field(payload, "session_id")

    provider: ProviderName | None = None
    if explicit in _SAFE_PROVIDERS:
        provider = cast(ProviderName, explicit)
    elif "/.codex/" in transcript_path:
        provider = "codex"
    elif "/.gemini/" in transcript_path:
        provider = "gemini"
    elif "/.pi/" in transcript_path:
        provider = "pi"
    elif event_name in _GEMINI_ONLY_EVENT_TYPES:
        provider = "gemini"
    elif (
        _str_field(payload, "permission_mode") or _str_field(payload, "model")
    ) and "/.claude/" not in transcript_path:
        # ``model``/``permission_mode`` are weak codex signals: Claude's Stop and
        # Notification payloads now also carry ``model``, so without this guard a
        # Claude session (transcript under ~/.claude/) installed with no
        # --provider flag is misdetected as codex.
        provider = "codex"
    elif _str_field(payload, "end_reason") or (
        session_id and not UUID_RE.match(session_id)
    ):
        provider = "pi"
    return provider
