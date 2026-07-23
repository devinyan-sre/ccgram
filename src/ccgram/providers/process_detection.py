"""Provider classification from a pane's foreground process.

All supported CLIs (claude, codex, gemini, pi) are Node.js scripts — the
multiplexer's ``pane_current_command`` shows ``bun`` or ``node`` instead of
the CLI name.  This module classifies the *foreground* process — resolved by
the multiplexer seam via ``Multiplexer.foreground(window_id)`` — to reliably
identify which provider is running.

The backend owns how the foreground process is read (tmux: ``ps -t <tty>``;
herdr: ``pane process-info``); this module never touches a tty or forks
``ps``.  It only classifies a ``ForegroundInfo.argv`` and caches the result.

Classification flow:
1. Skip wrapper tokens (sudo, env, node, bun, …) in the foreground argv
2. Match the first meaningful token against provider patterns / path markers
3. Cache by ``(window_id, fg_pgid)`` to skip re-classification when unchanged

Exposed entry points:
- ``classify_provider_from_argv`` / ``classify_provider_from_args`` — pure
  classification of a foreground argv
- ``detect_provider_cached`` — cached classification by foreground PGID
- ``clear_detection_cache`` — invalidate cache entries on cleanup
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog

from ..topic_state_registry import topic_state
from .shell import KNOWN_SHELLS as _KNOWN_SHELLS

if TYPE_CHECKING:
    from ..multiplexer.base import ForegroundInfo

logger = structlog.get_logger()

# Tokens that wrap the actual CLI binary — skip during classification.
_WRAPPER_TOKENS = frozenset(
    {"sudo", "env", "node", "bun", "npx", "bunx", "uv", "python", "python3"}
)

# Basename → provider name.  Checked via exact match and prefix (``claude-*``).
_PROVIDER_BASENAMES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"claude", "ce", "cc-mirror", "zai"}), "claude"),
    (frozenset({"codex"}), "codex"),
    (frozenset({"gemini"}), "gemini"),
    (frozenset({"pi"}), "pi"),
)

# Path substrings that identify a provider when basename alone is ambiguous
# (e.g. ``cli.js`` launched by bun).
_PROVIDER_PATH_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("claude-code", "cc-team"), "claude"),
    (("@openai/codex", "/codex/", "/codex-"), "codex"),
    (("gemini-cli",), "gemini"),
    (("@mariozechner/pi-coding-agent", "/pi-coding-agent/"), "pi"),
)


# JS runtimes that trigger ps-based detection in the caller.
JS_RUNTIMES = frozenset({"node", "bun", "npx", "bunx"})


def _match_token(token: str) -> str:
    """Match a single argv token against provider basenames and path markers."""
    basename = os.path.basename(token).lower().lstrip("-")

    # Direct basename match
    for names, provider in _PROVIDER_BASENAMES:
        if basename in names or basename.startswith(f"{provider}-"):
            return provider
    if basename in _KNOWN_SHELLS:
        return "shell"

    # Path-based match for ambiguous basenames (e.g. cli.js)
    token_lower = token.lower()
    for markers, provider in _PROVIDER_PATH_MARKERS:
        if any(m in token_lower for m in markers):
            return provider

    return ""


def classify_provider_from_argv(argv: Sequence[str]) -> str:
    """Classify provider from a foreground process's argv tokens.

    Skips wrapper tokens (``node``, ``bun``, ``sudo``, …) and matches the
    first meaningful token against known provider names or path markers.
    Returns provider name (``"claude"``, ``"codex"``, ``"gemini"``, ``"pi"``,
    ``"shell"``) or empty string if unrecognised.
    """
    for token in argv:
        cleaned = os.path.basename(token).lower().lstrip("-")
        if cleaned in _WRAPPER_TOKENS:
            continue
        return _match_token(token)

    return ""


def classify_provider_from_args(args: str) -> str:
    """Classify provider from a whitespace-joined argv string."""
    return classify_provider_from_argv(args.split())


# ---------------------------------------------------------------------------
# PGID-based cache
# ---------------------------------------------------------------------------

# window_id → (foreground_pgid, detected_provider_name)
_pgid_cache: dict[str, tuple[int, str]] = {}

# window_id → (monotonic_ts, ForegroundInfo | None): short-TTL cache over
# ``Multiplexer.foreground()``.  The per-tick discovery path otherwise forks
# ``ps -t`` (tmux) or ``pane process-info`` (herdr) once per window per
# second.  A restart of the CLI in a pane is still noticed within the TTL.
_fg_cache: dict[str, tuple[float, "ForegroundInfo | None"]] = {}

#: Seconds to reuse a foreground() answer before re-probing the backend.
FOREGROUND_TTL = 5.0


async def foreground_cached(window_id: str) -> ForegroundInfo | None:
    """Resolve ``multiplexer.foreground(window_id)`` through a short-TTL cache.

    Provider identity changes slowly (a pane runs one CLI for minutes), so a
    ``FOREGROUND_TTL``-second-old answer is fine for classification; restart
    detection degrades from ~1s to ≤``FOREGROUND_TTL`` s latency.
    """
    cached = _fg_cache.get(window_id)
    if cached is not None and time.monotonic() - cached[0] < FOREGROUND_TTL:
        return cached[1]

    # Lazy: multiplexer proxy resolves the active backend at call time;
    # importing at module load would wire-order-couple providers ↔ multiplexer.
    from ..multiplexer import multiplexer

    fg = await multiplexer.foreground(window_id)
    _fg_cache[window_id] = (time.monotonic(), fg)
    return fg


def get_cached_foreground_pgid(window_id: str) -> int:
    """Return the cached foreground PGID for a window, or 0 if unknown."""
    cached = _pgid_cache.get(window_id)
    return cached[0] if cached else 0


async def detect_provider_cached(window_id: str, fg: ForegroundInfo | None) -> str:
    """Classify the provider from a foreground process, cached by PGID.

    ``fg`` comes from ``Multiplexer.foreground(window_id)``.  When the
    foreground PGID matches the cached value, the more expensive
    ``classify_provider_from_argv`` is skipped.  Returns ``""`` when there is
    no foreground process to classify.
    """
    if fg is None or not fg.argv or fg.pgid == 0:
        return ""

    cached = _pgid_cache.get(window_id)
    if cached and cached[0] == fg.pgid:
        return cached[1]

    provider = classify_provider_from_argv(fg.argv)
    if provider:
        _pgid_cache[window_id] = (fg.pgid, provider)
    return provider


@topic_state.register("window")
def clear_detection_cache(window_id: str | None = None) -> None:
    """Clear cached detection for a window, or all windows if None."""
    if window_id is None:
        _pgid_cache.clear()
        _fg_cache.clear()
    else:
        _pgid_cache.pop(window_id, None)
        _fg_cache.pop(window_id, None)
