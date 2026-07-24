"""Operator-facing alerts: startup permission self-check + error aggregation.

Two concerns share one DM sink (the primary operator):

  1. Startup permission self-check — verify the bot has the "Manage Topics"
     admin right in each target chat, so a missing right surfaces at boot
     (with an actionable DM) instead of silently failing auto-topic-creation
     later. See ``check_group_permissions``.
  2. Error-rate alerting — an ``ErrorRateTracker`` counts ERROR-level log
     events by signature; when the same error fires ``threshold`` times inside
     ``window`` seconds it emits one alert (with a per-signature cooldown so a
     persistent fault doesn't spam). Wired as a structlog processor in
     ``main.py``; drained to the operator DM.

The pure pieces (``resolve_operator_chat_id``, ``ErrorRateTracker``, the
formatters) are unit-testable without a bot; the I/O pieces take a
``TelegramClient`` and are thin.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import structlog
from structlog.typing import EventDict

from .config import config
from .i18n import t
from .metrics import OPERATOR_ALERTS
from .telegram_client import TelegramClient

# Alert severity. Deliberately a plain string constant set rather than an enum:
# these values are metric label values (ccgram_operator_alerts{severity=...}),
# so their wire form is an operational contract that dashboards key on.
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

logger = structlog.get_logger()


# --------------------------------------------------------------------------
# Operator resolution + DM sink (shared)
# --------------------------------------------------------------------------


def resolve_operator_chat_id() -> int | None:
    """Resolve the primary-operator DM target.

    ``CCGRAM_OPERATOR_CHAT_ID`` wins; otherwise the lowest allowed-user id
    (deterministic, since ``allowed_users`` is an unordered set). Returns None
    when no operator can be determined (alerts are then skipped, not fatal).
    """
    if config.operator_chat_id is not None:
        return config.operator_chat_id
    if config.allowed_users:
        return min(config.allowed_users)
    return None


def resolve_operator_fallback_chat_id() -> int | None:
    """Resolve the fallback sink used when the primary DM can't be delivered.

    ``CCGRAM_OPERATOR_FALLBACK_CHAT_ID`` wins; otherwise the configured group
    (``CCGRAM_GROUP_ID``), where the bot is already a member. Returns None when
    neither is set. Config-driven — no chat id is ever hardcoded.
    """
    if config.operator_fallback_chat_id is not None:
        return config.operator_fallback_chat_id
    return config.group_id


def _operator_sinks() -> list[int]:
    """Ordered, de-duplicated delivery targets: primary DM, then fallback."""
    sinks: list[int] = []
    for chat_id in (resolve_operator_chat_id(), resolve_operator_fallback_chat_id()):
        if chat_id is not None and chat_id not in sinks:
            sinks.append(chat_id)
    return sinks


async def notify_operator(
    client: TelegramClient,
    text: str,
    *,
    severity: str = SEVERITY_WARNING,
) -> bool:
    """Deliver an operator alert, falling back from DM to the group sink.

    Tries the primary operator DM first; on any failure (e.g. the operator
    never opened a private chat, so Telegram refuses ``can't initiate
    conversation``) it tries the fallback sink so the alert still lands.
    Best-effort: returns True on first success, False if every sink fails or
    none is configured. Never propagates — alerting must not crash the caller.

    ``severity`` classifies the alert for metrics and log correlation; it does
    not change delivery. Every outcome — including "no sink configured" — is
    counted, so a silently unalertable deployment is visible on /metrics rather
    than looking identical to "no alerts fired".
    """
    sinks = _operator_sinks()
    if not sinks:
        logger.debug("No operator chat id resolved; skipping alert")
        OPERATOR_ALERTS.inc(severity=severity, outcome="no_sink")
        return False
    for index, chat_id in enumerate(sinks):
        is_last = index == len(sinks) - 1
        try:
            await client.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            OPERATOR_ALERTS.inc(
                severity=severity,
                outcome="sent" if index == 0 else "sent_fallback",
            )
            return True
        except Exception:  # noqa: BLE001 — alerting must never raise
            logger.warning(
                "Failed to deliver operator alert to %d%s",
                chat_id,
                "" if is_last else "; trying fallback sink",
                exc_info=is_last,
                severity=severity,
            )
    OPERATOR_ALERTS.inc(severity=severity, outcome="failed")
    return False


# --------------------------------------------------------------------------
# #2 Startup permission self-check
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionCheckResult:
    """Outcome of checking one chat's bot permissions."""

    chat_id: int
    ok: bool
    can_manage_topics: bool
    reason: str = ""


def _can_manage_topics(member: object) -> bool:
    """True if a ChatMember grants topic management.

    Chat owner (``creator``) implicitly has every right; an administrator must
    carry ``can_manage_topics``; anything else cannot manage topics.
    """
    status = getattr(member, "status", None)
    if status == "creator":
        return True
    if status == "administrator":
        return bool(getattr(member, "can_manage_topics", False))
    return False


async def check_group_permission(
    client: TelegramClient, chat_id: int, bot_id: int
) -> PermissionCheckResult:
    """Check the bot's Manage-Topics right in one chat.

    A failed API call (bot not in chat, network) yields ``ok=False`` with a
    reason rather than raising — the check is advisory.
    """
    try:
        member = await client.get_chat_member(chat_id=chat_id, user_id=bot_id)
    except Exception as exc:  # noqa: BLE001 — advisory check, never fatal
        return PermissionCheckResult(
            chat_id=chat_id, ok=False, can_manage_topics=False, reason=str(exc)
        )
    manage = _can_manage_topics(member)
    return PermissionCheckResult(chat_id=chat_id, ok=manage, can_manage_topics=manage)


def format_missing_permission_alert(result: PermissionCheckResult) -> str:
    """Render the operator DM for a chat missing the Manage-Topics right."""
    lines = [
        t("⚠️ *CCGram startup check*"),
        "",
        t("The bot is missing permissions in chat `{chat_id}`:").format(
            chat_id=result.chat_id
        ),
        t("• *Manage Topics* — required to auto-create and rename topics"),
        "",
        t("Grant it in the group's admin settings, then it recovers automatically."),
    ]
    return "\n".join(lines)


async def check_group_permissions(
    client: TelegramClient, chat_ids: list[int], bot_id: int
) -> list[PermissionCheckResult]:
    """Check every target chat; log + DM the operator for each missing right.

    Returns all results (ok and not) so callers/tests can assert. Sends at most
    one DM per problem chat; never raises.
    """
    results: list[PermissionCheckResult] = []
    for chat_id in chat_ids:
        result = await check_group_permission(client, chat_id, bot_id)
        results.append(result)
        if not result.ok:
            logger.warning(
                "Startup permission check: bot lacks Manage-Topics in chat %d%s",
                chat_id,
                f" ({result.reason})" if result.reason else "",
            )
            # Missing 'Manage Topics' blocks topic creation outright — the bot
            # keeps running but cannot do its job, so this is critical.
            await notify_operator(
                client,
                format_missing_permission_alert(result),
                severity=SEVERITY_CRITICAL,
            )
    return results


def format_operator_unreachable_alert(chat_id: int, fallback_chat_id: int) -> str:
    """Render the fallback-sink notice that the operator DM is undeliverable."""
    lines = [
        t("⚠️ *CCGram startup check*"),
        "",
        t("Can't DM the operator (`{chat_id}`): alerts are undeliverable.").format(
            chat_id=chat_id
        ),
        t(
            "Have that account send the bot `/start`, or point "
            "`CCGRAM_OPERATOR_CHAT_ID` at a chat the bot can post to."
        ),
        "",
        t("Until then, alerts are routed here (`{fallback}`).").format(
            fallback=fallback_chat_id
        ),
    ]
    return "\n".join(lines)


async def check_operator_reachable(client: TelegramClient) -> bool:
    """Startup self-check: verify the operator DM target is reachable.

    Probes with ``get_chat`` (no message sent). When the primary target is
    unreachable — the classic "bot can't initiate conversation" case, seen when
    the operator never opened a private chat — it logs an actionable hint and,
    if a distinct fallback sink exists, posts the notice there so the operator
    still learns their alert channel is broken. Never raises; returns True only
    when the primary target is confirmed reachable.
    """
    chat_id = resolve_operator_chat_id()
    if chat_id is None:
        logger.debug("No operator chat id configured; skipping reachability check")
        return False
    try:
        await client.get_chat(chat_id=chat_id)
        return True
    except Exception as exc:  # noqa: BLE001 — advisory check, never fatal
        fallback = resolve_operator_fallback_chat_id()
        logger.warning(
            "Operator alert target %d is unreachable (%s); DM alerts won't be "
            "delivered. Have that account send /start, or set "
            "CCGRAM_OPERATOR_CHAT_ID / CCGRAM_OPERATOR_FALLBACK_CHAT_ID to a "
            "chat the bot can post to.",
            chat_id,
            exc,
        )
        if fallback is not None and fallback != chat_id:
            try:
                await client.send_message(
                    chat_id=fallback,
                    text=format_operator_unreachable_alert(chat_id, fallback),
                    parse_mode="Markdown",
                )
            except Exception:  # noqa: BLE001 — advisory, never fatal
                logger.warning(
                    "Failed to post operator-unreachable notice to fallback %d",
                    fallback,
                    exc_info=True,
                )
        return False


# --------------------------------------------------------------------------
# #3 Error-rate aggregation
# --------------------------------------------------------------------------


@dataclass
class ErrorRateTracker:
    """Pure sliding-window error counter with per-signature cooldown.

    ``record(signature, now)`` returns the count that should be alerted on
    (``>= threshold`` inside ``window`` seconds, and outside the cooldown of
    the last alert for that signature), else ``0``. All time is injected so the
    tracker is deterministic under test.
    """

    threshold: int = 5
    window_seconds: float = 60.0
    cooldown_seconds: float = 600.0
    _events: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    _last_alert: dict[str, float] = field(default_factory=dict)

    def record(self, signature: str, now: float) -> int:
        stamps = self._events[signature]
        stamps.append(now)
        cutoff = now - self.window_seconds
        while stamps and stamps[0] < cutoff:
            stamps.popleft()
        if len(stamps) < self.threshold:
            return 0
        last = self._last_alert.get(signature)
        if last is not None and now - last < self.cooldown_seconds:
            return 0
        self._last_alert[signature] = now
        count = len(stamps)
        stamps.clear()  # reset so the next alert needs a fresh burst
        return count

    def reset(self) -> None:
        self._events.clear()
        self._last_alert.clear()


def format_error_alert(count: int, window_seconds: float, message: str) -> str:
    """Render the operator DM for an error burst."""
    return "\n".join(
        [
            t("🚨 *CCGram error alert*"),
            t("`{count}×` in {window}s: {message}").format(
                count=count, window=int(window_seconds), message=message
            ),
        ]
    )


def error_signature(event: str) -> str:
    """Collapse a log event to a stable signature for grouping.

    Trims to the leading text before the first digit-run so per-instance ids
    (window ids, chat ids, offsets) don't fragment otherwise-identical errors.
    """
    out: list[str] = []
    for ch in event:
        if ch.isdigit():
            break
        out.append(ch)
    sig = "".join(out).strip(" :=-#@")
    return sig or event[:80]


# --------------------------------------------------------------------------
# #3 wiring: structlog processor + operator DM sink
# --------------------------------------------------------------------------

_error_tracker = ErrorRateTracker()
_alert_client: TelegramClient | None = None


def set_error_alert_client(client: TelegramClient | None) -> None:
    """Arm (or disarm with None) the error-alert DM sink. Called at bootstrap."""
    global _alert_client
    _alert_client = client


def reset_error_alerts_for_testing() -> None:
    """Clear the sink and tracker state between tests."""
    global _alert_client
    _alert_client = None
    _error_tracker.reset()


def _schedule_operator_dm(text: str) -> None:
    """Fire-and-forget an operator DM on the running loop, if any.

    Called from a (possibly non-async) logging processor, so it must be
    thread-safe and never raise. No running loop → skip (e.g. startup logs).
    """
    client = _alert_client
    if client is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_soon_threadsafe(
        lambda: asyncio.ensure_future(
            notify_operator(client, text, severity=SEVERITY_CRITICAL)
        )
    )


def maybe_alert_error(method_name: str, event: str, *, now: float) -> int:
    """Feed one log event to the tracker; DM on a burst. Returns alerted count.

    Only ``error``/``critical`` levels count. Returns 0 (no alert) otherwise or
    below threshold. Pure-ish: time is injected; the only side effect is the
    scheduled DM.
    """
    if method_name not in ("error", "critical"):
        return 0
    if _alert_client is None:
        return 0
    count = _error_tracker.record(error_signature(event), now)
    if count <= 0:
        return 0
    _schedule_operator_dm(
        format_error_alert(count, _error_tracker.window_seconds, event[:200])
    )
    return count


def error_alert_processor(
    _logger: Any, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: pass-through that alerts on error bursts.

    Installed in ``main.setup_logging``. Never mutates ``event_dict`` and
    swallows its own failures so logging can't be broken by alerting.
    """
    try:
        event = event_dict.get("event")
        if isinstance(event, str):
            maybe_alert_error(method_name, event, now=time.monotonic())
    except Exception:  # noqa: BLE001 — a logging processor must never raise
        pass
    return event_dict
