"""Hook subcommand for Claude Code session and event tracking.

Called by Claude Code hooks (SessionStart, Notification, Stop, SubagentStart,
SubagentStop, TeammateIdle, TaskCompleted) to maintain a window↔session
mapping and an append-only event log.  Also provides `--install` to
auto-configure hooks in settings.json (respects CLAUDE_CONFIG_DIR).

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.ccgram_dir() (shared with config.py).
Claude settings path resolution uses CLAUDE_CONFIG_DIR env var (shared with config.py).

Key functions: hook_main() (CLI entry), _install_hook().
"""

import fcntl
import json
import logging
import os
import subprocess
import structlog
import sys
from pathlib import Path
from typing import Any

from ccgram.hooks.adapters import (
    detect_provider_from_payload,
    get_hook_adapter,
)
from ccgram.hooks.model import NormalizedHookEvent, ProviderName
from ccgram.multiplexer.self_identify import resolve_self_identity

# Installer / uninstaller / status live in ccgram.hooks.install; re-exported
# here so existing imports (doctor, bootstrap, tests) keep working.
from ccgram.hooks.install import (  # noqa: F401
    _CODEX_HOOK_EVENTS,
    _GEMINI_HOOK_EVENTS,
    _HOOK_EVENT_TYPES,
    _claude_settings_file,
    _codex_hooks_file,
    _current_hook_command,
    _gemini_settings_file,
    _hook_status,
    _install_hook,
    _json_hook_command_predicate,
    _uninstall_hook,
    get_installed_events,
)

logger = structlog.get_logger()

# Expected number of parts when parsing tmux display-message output.
# Minimum is 3 (session_name\t@id\twindow_name); a fourth pane_tty field is
# optional so older test mocks keep working with a 3-part stdout.
_TMUX_FORMAT_PARTS = 3
_TMUX_FORMAT_PARTS_WITH_TTY = 4

# ps -A output is split into 5 fields: pid, ppid, pgid, stat, command.
_PS_SNAPSHOT_FIELDS = 5


def _resolve_herdr_tab_id(pane_id: str) -> str | None:
    """Resolve a herdr pane id to its containing tab id.

    Runs ``herdr pane get <pane_id>`` and extracts ``result["pane"]["tab_id"]``.
    The socket path is picked up from ``$HERDR_SOCKET_PATH`` by the herdr CLI
    automatically (same as the multiplexer backend's subprocess runner).

    Returns None on any failure (herdr not installed, socket down, pane gone)
    so the caller degrades gracefully to the pane id.
    """
    try:
        result = subprocess.run(
            ["herdr", "pane", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("herdr pane get failed for pane %s: %s", pane_id, exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "herdr pane get returned non-zero for pane %s (rc=%d): %s",
            pane_id,
            result.returncode,
            result.stderr.strip(),
        )
        return None
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "herdr pane get returned unparseable JSON for pane %s: %s", pane_id, exc
        )
        return None
    if not isinstance(payload, dict):
        logger.warning(
            "herdr pane get returned unexpected type %s for pane %s",
            type(payload).__name__,
            pane_id,
        )
        return None
    tab_id = payload.get("result", {}).get("pane", {}).get("tab_id")
    if not isinstance(tab_id, str) or not tab_id:
        logger.warning(
            "herdr pane get missing tab_id for pane %s (payload=%r)", pane_id, payload
        )
        return None
    return tab_id


def _resolve_window_id(pane_id: str) -> tuple[str, str, str, str] | None:
    """Resolve tmux pane ID to (session_window_key, window_id, window_name, pane_tty).

    Returns None if resolution fails. pane_tty is the pane's controlling tty path
    (e.g. ``/dev/ttys012``) or "" when older tmux mocks omit the field.
    """
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                pane_id,
                "-p",
                "#{session_name}\t#{window_id}\t#{window_name}\t#{pane_tty}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        logger.warning("tmux display-message timed out for pane %s", pane_id)
        return None
    raw_output = result.stdout.strip()
    parts = raw_output.split("\t", 3)
    if len(parts) < _TMUX_FORMAT_PARTS:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux "
            "(pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return None

    tmux_session_name, window_id, window_name = parts[0], parts[1], parts[2]
    pane_tty = parts[3] if len(parts) >= _TMUX_FORMAT_PARTS_WITH_TTY else ""
    session_window_key = f"{tmux_session_name}:{window_id}"
    return session_window_key, window_id, window_name, pane_tty


def _ps_snapshot() -> dict[int, tuple[int, int, str, str]]:
    """Return ``{pid: (ppid, pgid, stat, command_basename)}`` for all processes.

    Empty dict on subprocess failure or unparseable output — callers must
    fail-open when the snapshot is empty.
    """
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired, OSError:
        return {}
    snapshot: dict[int, tuple[int, int, str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, _PS_SNAPSHOT_FIELDS - 1)
        if len(parts) < _PS_SNAPSHOT_FIELDS:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
        except ValueError:
            continue
        stat = parts[3]
        cmd_argv0 = parts[4].split(None, 1)[0] if parts[4] else ""
        cmd_base = cmd_argv0.rsplit("/", 1)[-1]
        snapshot[pid] = (ppid, pgid, stat, cmd_base)
    return snapshot


def _foreground_pgid_on_tty(
    snapshot: dict[int, tuple[int, int, str, str]], pane_tty: str
) -> int | None:
    """Return the foreground process group id on ``pane_tty``, or None."""
    if not pane_tty or not snapshot:
        return None
    tty_name = pane_tty.removeprefix("/dev/")
    if not tty_name:
        return None
    try:
        result = subprocess.run(
            ["ps", "-t", tty_name, "-o", "pid="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        info = snapshot.get(pid)
        if info and "+" in info[2]:
            return info[1]
    return None


def _closest_claude_ancestor(
    snapshot: dict[int, tuple[int, int, str, str]], start_pid: int
) -> int | None:
    """Walk parent chain from ``start_pid``; return the closest claude PID, or None."""
    cur = start_pid
    visited: set[int] = set()
    for _ in range(40):
        if cur <= 1 or cur in visited:
            return None
        visited.add(cur)
        info = snapshot.get(cur)
        if info is None:
            return None
        ppid, _pgid, _stat, cmd_base = info
        if cmd_base == "claude":
            return cur
        cur = ppid
    return None


def _is_nested_session(pane_tty: str) -> bool:
    """Return True if the hook was fired by a nested (non-foreground) claude.

    The "primary" claude in a tmux pane is launched by the user's shell, so
    its PID equals the foreground process group id on the pane's tty. Any
    claude spawned beneath that primary (e.g. an MCP-server-launched observer
    such as claude-mem) is a *descendant* — its PID differs from the
    foreground PGID even though it shares the pgid via inheritance.

    Fails open: returns False on any subprocess error or missing data so
    hook delivery is never made *more* fragile than the status quo.
    """
    if not pane_tty:
        return False
    snapshot = _ps_snapshot()
    if not snapshot:
        return False
    fg_pgid = _foreground_pgid_on_tty(snapshot, pane_tty)
    if fg_pgid is None:
        return False
    owner = _closest_claude_ancestor(snapshot, os.getpid())
    if owner is None:
        return False
    return owner != fg_pgid


def _write_event(
    event_type: str,
    session_id: str,
    window_key: str,
    data: dict[str, Any],
) -> None:
    """Append one JSONL event line to events.jsonl with file locking."""
    # Lazy: hook.py runs as `python -m ccgram.hook` from Claude Code on
    # every notification; deferring utils import until an event actually
    # fires keeps the latency-sensitive fast path lean.
    # Lazy: utils.ccgram_dir resolves $CCGRAM_DIR at runtime
    from .utils import ccgram_dir

    events_file = ccgram_dir() / "events.jsonl"
    events_file.parent.mkdir(parents=True, exist_ok=True)

    # Lazy: hooks.state_files only imported when an event fires (same rationale
    # as the utils import above: keep the hook fast path lean).
    from .hooks.state_files import serialize_event_record

    event_line = json.dumps(
        serialize_event_record(event_type, session_id, window_key, data),
        separators=(",", ":"),
    )

    try:
        with open(events_file, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(event_line + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        logger.exception("Failed to write event to %s", events_file)


def _update_session_map(
    session_window_key: str,
    session_id: str,
    cwd: str,
    window_name: str,
    transcript_path: str,
    tmux_session_name: str,
    provider_name: str = "claude",
) -> None:
    """Update session_map.json for a SessionStart event."""
    # Lazy: same hook fast-path rationale as ``_write_event``.
    from .utils import ccgram_dir, atomic_write_json

    map_file = ccgram_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        raw = map_file.read_text()
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            session_map = parsed
                        else:
                            logger.warning(
                                "session_map.json has unexpected type %s, ignoring",
                                type(parsed).__name__,
                            )
                    except json.JSONDecodeError:
                        # Corrupted JSON — preserve the file for inspection
                        # instead of silently overwriting with near-empty data.
                        backup = map_file.with_suffix(".json.corrupt")
                        try:
                            # Lazy: shutil only needed in the error path of
                            # backing up a corrupted session_map.json.
                            import shutil

                            shutil.copy2(map_file, backup)
                            logger.warning(
                                "Corrupted session_map.json backed up to %s",
                                backup,
                            )
                        except OSError:
                            logger.warning("Corrupted session_map.json (backup failed)")
                    except OSError:
                        logger.warning("Failed to read session_map.json")

                # Lazy: same hook fast-path rationale as _write_event.
                from .hooks.state_files import serialize_session_map_entry

                session_map[session_window_key] = serialize_session_map_entry(
                    session_id, cwd, window_name, transcript_path, provider_name
                )

                # Clean up old-format key ("session:window_name") if it exists
                old_key = f"{tmux_session_name}:{window_name}"
                if old_key != session_window_key and old_key in session_map:
                    del session_map[old_key]
                    logger.info("Removed old-format session_map key: %s", old_key)

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError:
        logger.exception("Failed to write session_map")


def _encode_pi_cwd_dirname(cwd: str) -> str:
    """Encode cwd using Pi's session directory convention."""
    stripped = cwd.lstrip("/\\").rstrip("/\\")
    encoded = stripped.replace("/", "-").replace("\\", "-").replace(":", "-")
    return f"--{encoded}--"


def _resolve_pi_transcript_path(session_id: str, cwd: str) -> str:
    """Find a Pi transcript path when hook-runner omitted it."""
    if not cwd:
        return ""
    session_dir = (
        Path.home() / ".pi" / "agent" / "sessions" / _encode_pi_cwd_dirname(cwd)
    )
    if not session_dir.is_dir():
        return ""
    candidates: list[tuple[float, Path]] = []
    try:
        for entry in session_dir.iterdir():
            if entry.suffix != ".jsonl" or not entry.is_file():
                continue
            try:
                candidates.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
    except OSError:
        return ""
    candidates.sort(reverse=True)
    for _mtime, path in candidates:
        if session_id and session_id in path.name:
            return str(path)
    return str(candidates[0][1]) if candidates else ""


def _resolve_transcript_path(
    provider_name: str, session_id: str, cwd: str, transcript_path: str
) -> str:
    """Return transcript path from payload or provider-specific fallback."""
    if provider_name == "pi":
        if transcript_path and session_id in Path(transcript_path).name:
            return transcript_path
        resolved = _resolve_pi_transcript_path(session_id, cwd)
        if resolved:
            if transcript_path and transcript_path != resolved:
                logger.warning(
                    "Ignoring stale Pi transcript path for session %s: %s -> %s",
                    session_id,
                    transcript_path,
                    resolved,
                )
            return resolved
        if transcript_path:
            return transcript_path
    elif transcript_path:
        return transcript_path
    return ""


def _read_session_map_entry(session_window_key: str) -> dict[str, str]:
    """Return the current session_map entry for ``session_window_key`` or {}."""
    # Lazy: same hook fast-path rationale as ``_write_event``.
    from .utils import ccgram_dir

    map_file = ccgram_dir() / "session_map.json"
    if not map_file.exists():
        return {}
    try:
        raw = json.loads(map_file.read_text())
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    entry = raw.get(session_window_key)
    return entry if isinstance(entry, dict) else {}


def _refresh_session_map_if_stale(
    session_window_key: str,
    session_id: str,
    provider_name: str,
    window_name: str,
    payload_cwd: str,
    payload_transcript_path: str,
) -> None:
    """Refresh ``session_map.json`` when a non-SessionStart event reports a
    different session_id or provider than the stored entry.

    Some installs (notably Pi via cc-thingz hook-runner) deliver Stop/Subagent
    hooks without a matching SessionStart through this hook path, so the map
    can keep pointing at the previous provider's session. We use values the
    hook payload already carries — no external scanning — to avoid the
    recovery anti-pattern called out in PR #51.
    """
    existing = _read_session_map_entry(session_window_key)
    if not existing:
        # SessionStart owns initial creation; never extend the map from a
        # non-SessionStart event. Missing entry means no prior session was
        # tracked here — leave the fallback (cwd-based discovery in
        # SessionMonitor) to handle it.
        return
    cwd = payload_cwd or existing.get("cwd", "")
    transcript_path = _resolve_transcript_path(
        provider_name, session_id, cwd, payload_transcript_path
    )
    if (
        existing.get("session_id") == session_id
        and existing.get("provider_name") == provider_name
        and (
            not transcript_path
            or existing.get("transcript_path", "") == transcript_path
        )
    ):
        return
    # Backend prefix token: split on the FIRST colon so herdr keys
    # ("herdr:w2:t1") yield "herdr", not "herdr:w2" (the tab id has a colon).
    tmux_session_name = session_window_key.split(":", 1)[0]
    _update_session_map(
        session_window_key,
        session_id,
        cwd,
        window_name,
        transcript_path,
        tmux_session_name,
        provider_name,
    )
    logger.info(
        "Refreshed stale session_map for %s: %s/%s -> %s/%s",
        session_window_key,
        existing.get("provider_name") or "<none>",
        (existing.get("session_id") or "<none>")[:8],
        provider_name,
        session_id[:8],
    )


def _provider_from_pane_tty(pane_tty: str) -> ProviderName | None:
    """Best-effort provider detection from foreground tty process commands.

    This is a last-resort fallback; the primary paths are the explicit
    ``provider_name`` field and the ``/.provider/`` transcript path prefix
    checked in ``detect_provider_from_payload``.  JS-wrapped Pi (e.g.
    ``node ~/.pi/agent/cli.js``) is not matched here — it is caught by the
    ``/.pi/`` transcript path check instead.
    """
    if not pane_tty:
        return None
    tty_name = pane_tty.removeprefix("/dev/")
    try:
        result = subprocess.run(
            ["ps", "-t", tty_name, "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    text = result.stdout.lower()
    if "gemini" in text:
        return "gemini"
    if "codex" in text:
        return "codex"
    if "claude" in text:
        return "claude"
    if any(tok == "pi" or tok.endswith("/pi") for tok in text.split()):
        return "pi"
    return None


def _locate_primary_window(
    session_id: str, event: str, provider_name: str = "claude"
) -> tuple[str, str, str] | None:
    """Resolve TMUX_PANE → primary window, or None to drop the hook.

    Returns ``(session_window_key, window_id, window_name)`` for the foreground
    claude in the pane. Returns ``None`` when the pane can't be resolved or
    when a nested claude (e.g. claude-mem observer) fired the hook — the
    nested case is logged at info so the rejection is visible to operators.

    Identity resolution is backend-neutral via ``resolve_self_identity``: tmux
    panes resolve through ``_resolve_window_id`` (``display-message``), herdr
    panes resolve pane→tab via ``_resolve_herdr_tab_id`` so the session_map key
    becomes ``herdr:<tab_id>`` (matching ``list_windows``).
    """
    identity = resolve_self_identity(
        os.environ,
        tmux_query=_resolve_window_id,
        herdr_query=_resolve_herdr_tab_id,
    )
    if identity is None:
        if not os.environ.get("TMUX_PANE") and not os.environ.get("HERDR_PANE_ID"):
            logger.warning(
                "Neither TMUX_PANE nor HERDR_PANE_ID set, cannot determine window"
            )
        elif os.environ.get("HERDR_PANE_ID"):
            logger.warning(
                "HERDR_PANE_ID=%s set but tab resolution failed "
                "(herdr not installed, socket down, or pane gone); "
                "hook event dropped",
                os.environ.get("HERDR_PANE_ID"),
            )
        return None
    logger.debug(
        "%s key=%s, window_name=%s, session_id=%s, event=%s",
        identity.mux,
        identity.session_window_key,
        identity.window_name,
        session_id,
        event,
    )
    # pane_tty is "" for herdr (no tty exposed), so _is_nested_session fails
    # open to False there — the nested-observer guard stays a tmux-only no-op.
    if provider_name == "claude" and _is_nested_session(identity.pane_tty):
        logger.info(
            "Skipping hook from nested claude (window_key=%s, session_id=%s, event=%s)",
            identity.session_window_key,
            session_id,
            event,
        )
        return None
    return identity.session_window_key, identity.window_id, identity.window_name


def _process_hook_stdin(
    provider_name: str | None = None,
) -> NormalizedHookEvent | None:
    """Process an agent hook event from stdin."""
    logger.debug("Processing hook event from stdin")
    try:
        raw_payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return None
    if not isinstance(raw_payload, dict):
        logger.warning("Hook stdin JSON must be an object")
        return None
    payload: dict[str, object] = raw_payload

    payload_provider = detect_provider_from_payload(payload)
    if provider_name and payload_provider and payload_provider != provider_name:
        logger.warning(
            "Hook --provider=%s but payload looks like %s; using %s",
            provider_name,
            payload_provider,
            provider_name,
        )
    detected_provider = provider_name or payload_provider
    if detected_provider is None:
        identity = resolve_self_identity(os.environ, tmux_query=_resolve_window_id)
        if identity:
            detected_provider = _provider_from_pane_tty(identity.pane_tty)
    if detected_provider is None:
        detected_provider = "claude"

    adapter = get_hook_adapter(detected_provider)
    if adapter is None:
        logger.debug("Ignoring hook for unsupported provider: %s", detected_provider)
        return None
    normalized = adapter.normalize(payload)
    if normalized is None:
        logger.debug(
            "Ignoring invalid hook payload for provider: %s", detected_provider
        )
        return None

    event = normalized.canonical_event_name
    if event not in _HOOK_EVENT_TYPES and event not in {"PreCompact", "PostCompact"}:
        logger.debug("Ignoring unhandled event: %s", event)
        return None

    located = _locate_primary_window(normalized.session_id, event, detected_provider)
    if located is None:
        return None
    session_window_key, _window_id, window_name = located

    if event == "SessionStart":
        # Backend prefix token (see _refresh_session_map_if_stale): split on the
        # first colon so herdr keys ("herdr:w2:t1") yield "herdr".
        tmux_session_name = session_window_key.split(":", 1)[0]
        transcript_path = _resolve_transcript_path(
            detected_provider,
            normalized.session_id,
            str(normalized.cwd) if normalized.cwd else "",
            str(normalized.transcript_path) if normalized.transcript_path else "",
        )
        cwd = str(normalized.cwd) if normalized.cwd else ""
        _update_session_map(
            session_window_key,
            normalized.session_id,
            cwd,
            window_name,
            transcript_path,
            tmux_session_name,
            detected_provider,
        )
        data = dict(normalized.data)
        data.update(
            {
                "cwd": cwd,
                "transcript_path": transcript_path,
                "window_name": window_name,
            }
        )
        _write_event(event, normalized.session_id, session_window_key, data)
        return normalized

    _refresh_session_map_if_stale(
        session_window_key,
        normalized.session_id,
        detected_provider,
        window_name,
        str(normalized.cwd) if normalized.cwd else "",
        str(normalized.transcript_path) if normalized.transcript_path else "",
    )
    _write_event(event, normalized.session_id, session_window_key, normalized.data)
    return normalized


def _configure_hook_logging() -> None:
    """Keep hook diagnostics off stdout, which some providers parse as protocol."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def hook_main(
    install: bool = False,
    uninstall: bool = False,
    status: bool = False,
    provider_name: str = "claude",
) -> None:
    """Process a Claude Code hook event from stdin, or manage hook installation."""
    _configure_hook_logging()

    if install:
        logger.info("Hook install requested")
        sys.exit(_install_hook(provider_name))

    if uninstall:
        sys.exit(_uninstall_hook(provider_name))

    if status:
        sys.exit(_hook_status(provider_name))

    # Pass None for the implicit Claude default so detect_provider_from_payload
    # gets first say (an explicit `--provider claude` invocation deliberately
    # keeps the explicit flag to surface the mismatch warning when payload
    # heuristics disagree). The CLI default also resolves to "claude", so the
    # None path covers the common case of an unannotated hook command.
    normalized = _process_hook_stdin(
        provider_name if provider_name != "claude" else None
    )
    if (
        normalized
        and normalized.provider_name == "codex"
        and (normalized.canonical_event_name == "Stop")
    ):
        print("{}")
