"""Audit trail + operator alerting for irreversible actions.

On 2026-07-25 a tmux server restart flipped every bound window to "dead" at
once; ten minutes later the autoclose sweep deleted four forum topics, and
Telegram destroys a topic's whole message history along with it. Nothing about
that was visible: every destructive path logged at ``info``, and
``operator_alerts.maybe_alert_error`` only reacts to ``error``/``critical``
*bursts*. The loss was found by a human noticing an empty topic days later.

This module is the single choke point those paths now route through:

  - one ``warning``-level structured log line per action, tagged
    ``audit="destructive"`` so the trail is greppable and stands out from the
    ordinary ``info`` stream;
  - one ``ccgram_destructive_actions`` metric sample, so a rising unattended
    destruction rate is visible on /metrics before anyone notices damage;
  - for unattended actions only (``actor="auto"``), an immediate operator DM —
    unconditional and un-deduplicated, because the whole failure mode here is
    a *single* silent event, not a burst.

User-initiated destruction (``actor="user"`` — the ``/sessions`` kill button,
``/sync`` fixes) is recorded and counted but never DM'd: the user is looking
right at it, and alerting on it would train the operator to ignore the channel.

The formatter and ``DestructiveAction`` are pure; delivery goes through
``operator_alerts.notify_operator`` (primary DM → group fallback). The client
is set once at bootstrap rather than threaded through every call site —
``_kill_expired_unbound`` has no ``TelegramClient`` in scope, and mirroring
``set_error_alert_client`` keeps the two sinks wired the same way.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .config import config
from .i18n import t
from .metrics import DESTRUCTIVE_ACTIONS
from .telegram_client import TelegramClient

logger = structlog.get_logger()

# Who caused the action. Metric label values, so their wire form is a contract.
ACTOR_AUTO = "auto"
ACTOR_USER = "user"

# Action identifiers. Kept as constants so call sites can't drift from the
# label values dashboards and alert rules key on.
ACTION_TOPIC_RETIRED = "topic_retired"
ACTION_TOPIC_REMOVED_SYNC = "topic_removed_sync"
ACTION_WINDOW_KILLED_UNBOUND = "window_killed_unbound"
ACTION_WINDOW_KILLED_TOPIC_GONE = "window_killed_topic_gone"
ACTION_WINDOW_KILLED_BY_USER = "window_killed_by_user"

_audit_client: TelegramClient | None = None


def set_audit_client(client: TelegramClient | None) -> None:
    """Arm (or disarm) the operator sink for destructive-action alerts."""
    global _audit_client
    _audit_client = client


@dataclass(frozen=True)
class DestructiveAction:
    """One irreversible action, ready to be logged, counted and rendered."""

    action: str
    actor: str
    detail: str = ""
    window_id: str | None = None
    thread_id: int | None = None
    user_id: int | None = None

    @property
    def is_unattended(self) -> bool:
        """True when nobody asked for this action at the moment it happened."""
        return self.actor == ACTOR_AUTO


# Human-readable one-liners. Deliberately state what was *lost*, not just what
# ran — an operator reading a phone notification needs the consequence first.
_ACTION_TEXT = {
    ACTION_TOPIC_RETIRED: "Topic retired by autoclose",
    ACTION_TOPIC_REMOVED_SYNC: "Topic removed by /sync (history destroyed)",
    ACTION_WINDOW_KILLED_UNBOUND: "Window killed — unbound past its TTL",
    ACTION_WINDOW_KILLED_TOPIC_GONE: "Window killed — its topic was deleted",
    ACTION_WINDOW_KILLED_BY_USER: "Window killed from the sessions dashboard",
}


def describe_action(action: str) -> str:
    """Render an action id as a translated one-liner (unknown ids pass through)."""
    text = _ACTION_TEXT.get(action)
    return t(text) if text else action


def format_destructive_alert(event: DestructiveAction) -> str:
    """Render the operator DM for one unattended destructive action."""
    lines = [
        t("🔻 *CCGram automatic action*"),
        "",
        describe_action(event.action),
    ]
    if event.detail:
        lines.append(event.detail)
    context: list[str] = []
    if event.window_id:
        context.append(f"window `{event.window_id}`")
    if event.thread_id is not None:
        context.append(f"topic `{event.thread_id}`")
    if context:
        lines.append(" · ".join(context))
    lines += [
        "",
        t("Nobody requested this — it was automatic cleanup. Check it was correct."),
    ]
    return "\n".join(lines)


async def record_destructive(
    action: str,
    *,
    actor: str,
    detail: str = "",
    window_id: str | None = None,
    thread_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """Log, count, and (for unattended actions) DM one irreversible action.

    Best-effort and never raises: an audit sink that can break the operation it
    audits would be worse than no sink at all.
    """
    event = DestructiveAction(
        action=action,
        actor=actor,
        detail=detail,
        window_id=window_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    # Warning, not info: an irreversible action deserves to stand out in the
    # log even when it is entirely expected. `maybe_alert_error` ignores
    # warnings, so this cannot double-fire with error-burst alerting.
    logger.warning(
        "destructive_action",
        audit="destructive",
        action=event.action,
        actor=event.actor,
        window_id=event.window_id,
        thread_id=event.thread_id,
        user_id=event.user_id,
        detail=event.detail or None,
    )
    DESTRUCTIVE_ACTIONS.inc(action=event.action, actor=event.actor)

    if not event.is_unattended or not config.destructive_alerts_enabled:
        return
    client = _audit_client
    if client is None:
        return
    # Lazy: operator_alerts imports this module's siblings; keeping the import
    # here also lets tests exercise the log/metric path without the DM sink.
    from .operator_alerts import SEVERITY_WARNING, notify_operator

    await notify_operator(
        client, format_destructive_alert(event), severity=SEVERITY_WARNING
    )
