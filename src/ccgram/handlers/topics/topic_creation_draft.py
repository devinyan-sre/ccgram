"""Typed accessor for the per-user directory-browser / topic-creation flow state.

All 15 user_data keys used across the directory browser, worktree picker, workspace
picker, and window-launch flow are centralised here.  ``TopicCreationDraft`` wraps a
``context.user_data`` dict and exposes typed properties so call sites don't scatter
raw string literals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..callback_helpers import get_thread_id
from ..user_state import (
    AWAITING_WORKTREE_BRANCH_NAME,
    PENDING_LAUNCH_THREAD,
    PENDING_THREAD_ID,
    PENDING_THREAD_TEXT,
    PENDING_WORKSPACE_ID,
    PENDING_WORKSPACES,
    PENDING_WORKTREE_BRANCH,
    PENDING_WORKTREE_CREATING,
    PENDING_WORKTREE_DIRTY,
    PENDING_WORKTREE_PATH,
    PENDING_WORKTREE_REPO,
    PENDING_WORKTREE_SUBDIR,
)
from .directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

# Re-export all key names so importers can use this module as the single source.
__all__ = [
    "AWAITING_WORKTREE_BRANCH_NAME",
    "BROWSE_DIRS_KEY",
    "BROWSE_PAGE_KEY",
    "BROWSE_PATH_KEY",
    "PENDING_LAUNCH_THREAD",
    "PENDING_THREAD_ID",
    "PENDING_THREAD_TEXT",
    "PENDING_WORKSPACES",
    "PENDING_WORKSPACE_ID",
    "PENDING_WORKTREE_BRANCH",
    "PENDING_WORKTREE_CREATING",
    "PENDING_WORKTREE_DIRTY",
    "PENDING_WORKTREE_PATH",
    "PENDING_WORKTREE_REPO",
    "PENDING_WORKTREE_SUBDIR",
    "_browser_flow_stale",
    "_launch_in_progress",
    "_required_selected_path",
    "clear_launch_in_progress",
    "mark_launch_in_progress",
]


# ── module-level helpers ─────────────────────────────────────────────────────


def _browser_flow_stale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the directory-browser flow was reset or the tap is cross-topic.

    A live browser always has ``PENDING_THREAD_ID`` set in the same topic
    (``_handle_unbound_topic`` / ``_handle_dead_window`` set it together
    with the browse state; navigation never clears it). If it is gone
    (``/start`` or Cancel cleared it) or the tap arrived in a different
    topic, every navigation/favorites handler must fail closed: otherwise
    they repopulate ``BROWSE_PATH_KEY`` (falling back to the bot's own
    cwd) *without* setting ``STATE_KEY``, so ``_check_ui_guards`` can't
    catch the residue and a later stale ``db:confirm`` spawns a window in
    that path. ``_handle_star`` would also toggle a persistent favorite
    off a dead browser.
    """
    pending_tid = (
        context.user_data.get(PENDING_THREAD_ID) if context.user_data else None
    )
    return pending_tid is None or get_thread_id(update) != pending_tid


def _required_selected_path(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Selected directory for a window-creating step, or None if the flow
    was reset (e.g. by ``/start``, which clears ``BROWSE_PATH_KEY``).

    Unlike the navigation handlers, the create path must never fall back
    to the bot's cwd: a stale provider/worktree button tapped after a
    reset would otherwise spawn an unbound tmux window running an agent
    CLI in the bot's own working directory.
    """
    if context.user_data is None:
        return None
    path = context.user_data.get(BROWSE_PATH_KEY)
    return path if isinstance(path, str) and path else None


# ── window-launch in-flight guard ────────────────────────────────────────────
#
# launch_window takes several seconds (tmux window creation, shell/hook
# readiness). The launch handlers clear the browse state *before* awaiting it,
# so a duplicate tap on the picker during that window sees no selected path and
# would otherwise misreport "Selection expired" even though creation succeeds.


def mark_launch_in_progress(user_data: dict | None, thread_id: int | None) -> None:
    """Record that launch_window is running for ``thread_id``."""
    if user_data is not None:
        user_data[PENDING_LAUNCH_THREAD] = thread_id


def clear_launch_in_progress(user_data: dict | None) -> None:
    """Release the in-flight launch marker (call in ``finally``)."""
    if user_data is not None:
        user_data.pop(PENDING_LAUNCH_THREAD, None)


def _launch_in_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if a window launch is already running for the tapped topic."""
    if context.user_data is None or PENDING_LAUNCH_THREAD not in context.user_data:
        return False
    return context.user_data[PENDING_LAUNCH_THREAD] == get_thread_id(update)
