"""Hook installer / uninstaller / status CLI for agent providers.

Extracted from ``hook.py`` (the runtime hook entry point) so the install
lifecycle — settings.json (Claude), hooks.json + config.toml (Codex),
settings.json (Gemini) — lives beside the other provider-aware hook
support in ``ccgram.hooks``. Pure stdlib + adapters: must NOT import
``config.py`` (hooks run inside agent panes without bot env vars).

Key functions: install_hook(), uninstall_hook(), hook_status(),
get_installed_events(). Private helpers keep their original names so the
``ccgram.hook`` re-exports stay patch-compatible.
"""

import json
import os
import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapters import get_hook_adapter


def _claude_settings_file() -> Path:
    """Resolve Claude settings.json path, respecting CLAUDE_CONFIG_DIR."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "settings.json"
    return Path.home() / ".claude" / "settings.json"


# Current hook command uses the active Python interpreter to avoid PATH issues.
_CURRENT_HOOK_MARKER = "ccgram.main hook"
# Older installs used the console script name directly.
_PATH_HOOK_MARKER = "ccgram hook"


# Hook event types ccgram handles (order matters for status display)
_HOOK_EVENT_TYPES: tuple[str, ...] = (
    "SessionStart",
    "Notification",
    "Stop",
    "StopFailure",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "TeammateIdle",
    "TaskCompleted",
)

# Events that should not block the agent (async: true)
_ASYNC_EVENTS: frozenset[str] = frozenset(
    {
        "StopFailure",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
        "TeammateIdle",
        "TaskCompleted",
    }
)


def _installable_events_for(provider_name: str) -> tuple[str, ...]:
    """Pull installable_events from an adapter, asserting it exists."""
    adapter = get_hook_adapter(provider_name)
    if adapter is None:
        raise AssertionError(f"no hook adapter registered for {provider_name!r}")
    return adapter.installable_events


# Source of truth: each adapter declares its installable_events. We re-export
# under the legacy names so existing call sites in doctor_cmd keep working
# without a churny import migration.
_CODEX_HOOK_EVENTS: tuple[str, ...] = _installable_events_for("codex")
_GEMINI_HOOK_EVENTS: tuple[str, ...] = _installable_events_for("gemini")


def _codex_hooks_file() -> Path:
    """Return the user-level Codex hooks.json path."""
    return Path.home() / ".codex" / "hooks.json"


def _codex_config_file() -> Path:
    """Return the user-level Codex config.toml path."""
    return Path.home() / ".codex" / "config.toml"


def _gemini_settings_file() -> Path:
    """Return the user-level Gemini settings.json path."""
    return Path.home() / ".gemini" / "settings.json"


def _current_hook_command(provider_name: str = "claude") -> str:
    """Build the hook command bound to the current Python interpreter."""
    command = f"{shlex.quote(sys.executable)} -m ccgram.main hook"
    if provider_name != "claude":
        command += f" --provider {shlex.quote(provider_name)}"
    return command


def _is_current_hook_command(command: str) -> bool:
    """Return True when the command matches the current module-based hook style."""
    return _CURRENT_HOOK_MARKER in command


def _is_any_ccgram_hook_command(command: str) -> bool:
    """Return True for current, old, or legacy hook command styles."""
    return any(
        marker in command for marker in (_CURRENT_HOOK_MARKER, _PATH_HOOK_MARKER)
    )


def _has_matching_hook(
    settings: dict, event_type: str, predicate: Callable[[str], bool]
) -> bool:
    """Check if an event has a hook command matching the predicate."""
    hooks = settings.get("hooks", {})
    event_hooks = hooks.get(event_type, [])

    for entry in event_hooks:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if predicate(cmd):
                return True
    return False


def _has_ccgram_hook(settings: dict, event_type: str) -> bool:
    """Check if ccgram hook is installed."""
    return _has_matching_hook(settings, event_type, _is_any_ccgram_hook_command)


def get_installed_events(settings: dict) -> dict[str, bool]:
    """Return installation status for each expected hook event type."""
    return {event: _has_ccgram_hook(settings, event) for event in _HOOK_EVENT_TYPES}


def _replace_hook_commands(
    settings: dict, event_type: str, predicate: Callable[[str], bool], replacement: str
) -> None:
    """Replace matching hook commands for an event with the given command."""
    event_hooks = settings.get("hooks", {}).get(event_type, [])
    for entry in event_hooks:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if predicate(cmd):
                h["command"] = replacement


def _load_json_settings(path: Path) -> dict[str, Any] | None:
    """Load a JSON settings file, returning an empty dict when absent."""
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(parsed, dict):
        print(f"Error reading {path}: expected JSON object", file=sys.stderr)
        return None
    return parsed


def _json_hook_command_predicate(provider_name: str) -> Callable[[str], bool]:
    """Build predicate for provider-specific ccgram hook commands.

    Matches `--provider {name}` as a whole token so e.g. `--provider codex-dev`
    does not also match `--provider codex`. We append a trailing space to the
    command so a token at the very end of the string also matches the
    space-delimited needle.
    """

    needle = f" --provider {provider_name} "

    def _predicate(command: str) -> bool:
        return _is_any_ccgram_hook_command(command) and needle in f" {command} "

    return _predicate


def _hook_entry(provider_name: str, timeout_value: int) -> dict[str, Any]:
    """Build a command hook entry for non-Claude providers.

    ``timeout_value`` is provider-defined: Codex hooks.json uses seconds,
    Gemini settings.json uses milliseconds. Callers must pass the unit the
    target schema expects.
    """
    return {
        "name": "ccgram-session-tracker",
        "type": "command",
        "command": _current_hook_command(provider_name),
        "timeout": timeout_value,
    }


def _install_json_hooks(
    path: Path, provider_name: str, events: tuple[str, ...], timeout_value: int
) -> int:
    """Install ccgram command hooks into a JSON settings file.

    ``timeout_value`` is provider-defined (seconds for Codex, ms for Gemini).
    """
    settings = _load_json_settings(path)
    if settings is None:
        return 1
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"Error reading {path}: hooks must be an object", file=sys.stderr)
        return 1

    installed_count = 0
    already_count = 0
    predicate = _json_hook_command_predicate(provider_name)
    for event_type in events:
        event_hooks = hooks.setdefault(event_type, [])
        if not isinstance(event_hooks, list):
            print(
                f"Error reading {path}: hooks.{event_type} must be an array",
                file=sys.stderr,
            )
            return 1
        if _has_matching_hook(settings, event_type, predicate):
            already_count += 1
            continue
        event_hooks.append({"hooks": [_hook_entry(provider_name, timeout_value)]})
        installed_count += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Lazy: utils brings in subprocess + structlog at import time; not worth
        # paying that cost on `ccgram --help`, only at hook-install.
        from ..utils import atomic_write_json

        atomic_write_json(path, settings)
    except OSError as e:
        print(f"Error writing {path}: {e}", file=sys.stderr)
        return 1
    print(
        f"{provider_name} hooks installed in {path}: "
        f"{installed_count} new, {already_count} already present"
    )
    return 0


_CODEX_HOOKS_KEY_RE = re.compile(r"^\s*codex_hooks\s*=\s*(\S+)", re.MULTILINE)


def _ensure_codex_feature_flag() -> int:
    """Ensure user Codex config enables the hooks feature flag.

    Detects any existing ``codex_hooks =`` line (spacing-tolerant). If it's
    already truthy, no-op. If it's explicitly false, warn and refuse to
    overwrite — the user opted out. Otherwise insert under ``[features]``.
    """
    config_file = _codex_config_file()
    if not config_file.exists():
        try:
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text("[features]\ncodex_hooks = true\n")
        except OSError as e:
            print(f"Error creating {config_file}: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        text = config_file.read_text()
    except OSError as e:
        print(f"Error reading {config_file}: {e}", file=sys.stderr)
        return 1
    match = _CODEX_HOOKS_KEY_RE.search(text)
    if match:
        value = match.group(1).rstrip(",")
        if value == "true":
            return 0
        print(
            f"{config_file} has codex_hooks = {value}; set it to true and rerun.",
            file=sys.stderr,
        )
        return 1
    if "[features]" in text:
        text = text.replace("[features]", "[features]\ncodex_hooks = true", 1)
    else:
        text = text.rstrip() + "\n\n[features]\ncodex_hooks = true\n"
    try:
        config_file.write_text(text)
    except OSError as e:
        print(f"Error writing {config_file}: {e}", file=sys.stderr)
        return 1
    return 0


_CODEX_HOOK_TIMEOUT_SECONDS = 5
_GEMINI_HOOK_TIMEOUT_MS = 5_000


def _install_codex_hook() -> int:
    """Install user-level Codex hooks and enable the feature flag."""
    if _ensure_codex_feature_flag() != 0:
        return 1
    return _install_json_hooks(
        _codex_hooks_file(), "codex", _CODEX_HOOK_EVENTS, _CODEX_HOOK_TIMEOUT_SECONDS
    )


def _install_gemini_hook() -> int:
    """Install user-level Gemini hooks."""
    return _install_json_hooks(
        _gemini_settings_file(), "gemini", _GEMINI_HOOK_EVENTS, _GEMINI_HOOK_TIMEOUT_MS
    )


def _uninstall_json_hooks(path: Path, provider_name: str) -> int:
    """Remove provider-specific ccgram hooks from a JSON settings file."""
    settings = _load_json_settings(path)
    if settings is None:
        return 1
    if not settings:
        print(f"No {path} found — nothing to uninstall.")
        return 0
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return 0
    predicate = _json_hook_command_predicate(provider_name)
    removed = 0
    for event_hooks in hooks.values():
        if not isinstance(event_hooks, list):
            continue
        for group in event_hooks:
            if not isinstance(group, dict):
                continue
            inner_hooks = group.get("hooks", [])
            if not isinstance(inner_hooks, list):
                continue
            kept = []
            for hook_config in inner_hooks:
                if isinstance(hook_config, dict) and predicate(
                    hook_config.get("command", "")
                ):
                    removed += 1
                    continue
                kept.append(hook_config)
            group["hooks"] = kept
    if removed == 0:
        print(f"No {provider_name} hooks found in {path} — nothing to remove.")
        return 0
    try:
        # Lazy: same rationale as _install_json_hooks.
        from ..utils import atomic_write_json

        atomic_write_json(path, settings)
    except OSError as e:
        print(f"Error writing {path}: {e}", file=sys.stderr)
        return 1
    print(f"{provider_name} hooks removed from {path}: {removed}")
    return 0


def _json_hook_status(path: Path, provider_name: str, events: tuple[str, ...]) -> int:
    """Print provider-specific JSON hook status."""
    settings = _load_json_settings(path)
    if settings is None:
        return 1
    if not settings:
        print(f"Not installed ({path} does not exist)")
        return 1
    predicate = _json_hook_command_predicate(provider_name)
    statuses = {
        event_type: _has_matching_hook(settings, event_type, predicate)
        for event_type in events
    }
    for event_type, installed in statuses.items():
        status_str = "installed" if installed else "MISSING"
        print(f"  {event_type}: {status_str}")
    if all(statuses.values()):
        print("All hooks installed")
        return 0
    missing = [
        event_type for event_type, installed in statuses.items() if not installed
    ]
    print(f"Missing hooks: {', '.join(missing)}")
    return 1


def _install_hook(provider_name: str = "claude") -> int:  # noqa: PLR0912
    """Install ccgram hooks for all event types into provider settings.

    Returns 0 on success, 1 on error.
    """
    match provider_name:
        case "codex":
            return _install_codex_hook()
        case "gemini":
            return _install_gemini_hook()
        case "pi":
            print(
                "Pi hooks are provided by the hook-runner extension; nothing to install."
            )
            return 0
        case "claude":
            pass
        case _:
            print(f"Unsupported hook provider: {provider_name}", file=sys.stderr)
            return 1
    settings_file = _claude_settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    if "hooks" not in settings:
        settings["hooks"] = {}

    installed_count = 0
    already_count = 0
    current_command = _current_hook_command("claude")

    for event_type in _HOOK_EVENT_TYPES:
        has_current = _has_matching_hook(settings, event_type, _is_current_hook_command)
        has_known = _has_matching_hook(
            settings, event_type, _is_any_ccgram_hook_command
        )

        if has_known and not has_current:
            _replace_hook_commands(
                settings,
                event_type,
                _is_any_ccgram_hook_command,
                current_command,
            )
            installed_count += 1
            continue

        if has_current:
            already_count += 1
            continue

        hook_config: dict[str, Any] = {
            "type": "command",
            "command": current_command,
            "timeout": 5,
        }
        if event_type in _ASYNC_EVENTS:
            hook_config["async"] = True

        if event_type not in settings["hooks"]:
            settings["hooks"][event_type] = []

        event_hooks = settings["hooks"][event_type]
        if event_hooks:
            first_entry = event_hooks[0]
            if isinstance(first_entry, dict):
                first_entry.setdefault("hooks", []).append(hook_config)
            else:
                event_hooks.append({"hooks": [hook_config]})
        else:
            event_hooks.append({"hooks": [hook_config]})

        installed_count += 1

    if installed_count == 0 and already_count == len(_HOOK_EVENT_TYPES):
        print(f"All hooks already installed in {settings_file}")
        return 0

    # Write back
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    print(
        f"Hooks installed in {settings_file}: "
        f"{installed_count} new, {already_count} already present"
    )
    return 0


def _uninstall_hook(provider_name: str = "claude") -> int:  # noqa: PLR0911
    """Remove ccgram hooks from provider settings.

    Returns 0 on success, 1 on error.
    """
    match provider_name:
        case "codex":
            return _uninstall_json_hooks(_codex_hooks_file(), "codex")
        case "gemini":
            return _uninstall_json_hooks(_gemini_settings_file(), "gemini")
        case "pi":
            print(
                "Pi hooks are managed by the hook-runner extension; nothing to uninstall."
            )
            return 0
        case "claude":
            pass
        case _:
            print(f"Unsupported hook provider: {provider_name}", file=sys.stderr)
            return 1
    settings_file = _claude_settings_file()
    if not settings_file.exists():
        print("No settings.json found — nothing to uninstall.")
        return 0

    try:
        settings = json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {settings_file}: {e}", file=sys.stderr)
        return 1

    # Check if any ccgram hooks are installed
    any_installed = any(
        _has_ccgram_hook(settings, event) for event in _HOOK_EVENT_TYPES
    )
    if not any_installed:
        print("Hook not installed — nothing to uninstall.")
        return 0

    # Remove ccgram hook entries from all event types
    hooks_section = settings.get("hooks", {})
    for event_type in _HOOK_EVENT_TYPES:
        event_hooks = hooks_section.get(event_type, [])
        if not event_hooks:
            continue

        new_event_hooks = []
        for entry in event_hooks:
            if not isinstance(entry, dict):
                new_event_hooks.append(entry)
                continue
            inner_hooks = entry.get("hooks", [])
            filtered = [
                h
                for h in inner_hooks
                if not isinstance(h, dict)
                or not _is_any_ccgram_hook_command(h.get("command", ""))
            ]
            if filtered:
                entry["hooks"] = filtered
                new_event_hooks.append(entry)

        hooks_section[event_type] = new_event_hooks

    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    print(f"Hooks uninstalled from {settings_file}")
    return 0


def _hook_status(provider_name: str = "claude") -> int:
    """Show per-event hook installation status.

    Returns 0 if all installed, 1 if any missing.
    """
    match provider_name:
        case "codex":
            return _json_hook_status(_codex_hooks_file(), "codex", _CODEX_HOOK_EVENTS)
        case "gemini":
            return _json_hook_status(
                _gemini_settings_file(), "gemini", _GEMINI_HOOK_EVENTS
            )
        case "pi":
            print("Pi hook status depends on the hook-runner extension.")
            print(
                "Expected built-in hook-runner ccgram events: "
                "SessionStart, Stop, SessionEnd, SubagentStart"
            )
            return 0
        case "claude":
            pass
        case _:
            print(f"Unsupported hook provider: {provider_name}", file=sys.stderr)
            return 1
    settings_file = _claude_settings_file()
    if not settings_file.exists():
        print(f"Not installed ({settings_file} does not exist)")
        return 1

    try:
        settings = json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {settings_file}: {e}", file=sys.stderr)
        return 1

    event_status = get_installed_events(settings)
    all_installed = all(event_status.values())

    for event_type, installed in event_status.items():
        status_str = "installed" if installed else "MISSING"
        print(f"  {event_type}: {status_str}")

    if all_installed:
        print("All hooks installed")
        return 0

    missing = [e for e, v in event_status.items() if not v]
    print(f"Missing hooks: {', '.join(missing)}")
    return 1
