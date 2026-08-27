"""Configured time-zone helpers for human-facing timestamps.

Internal persistence, comparisons, leases, and audit records continue using
UTC/epoch time. Only text shown to operators, timestamped upload names, and
explicitly local schedules use these helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import config


def display_timezone() -> tzinfo:
    """Return the configured IANA timezone, safely falling back to UTC."""
    try:
        return ZoneInfo(config.timezone_name)
    except ZoneInfoNotFoundError:
        return UTC


def now_display() -> datetime:
    """Current aware datetime in the configured operator-facing timezone."""
    return datetime.now(display_timezone())


def localize_timestamp(value: datetime) -> datetime:
    """Convert an aware or UTC-assumed naive datetime for operator display."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(display_timezone())


def format_epoch(value: int | float, pattern: str) -> str:
    """Format epoch seconds in the configured operator-facing timezone."""
    return datetime.fromtimestamp(value, display_timezone()).strftime(pattern)


__all__ = [
    "display_timezone",
    "format_epoch",
    "localize_timestamp",
    "now_display",
]
