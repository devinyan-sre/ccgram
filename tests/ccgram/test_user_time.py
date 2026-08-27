from datetime import UTC, datetime

from ccgram.config import config
from ccgram.user_time import format_epoch, localize_timestamp


def test_epoch_is_formatted_in_configured_timezone(monkeypatch) -> None:
    monkeypatch.setattr(config, "timezone_name", "Asia/Shanghai")
    assert format_epoch(0, "%Y-%m-%d %H:%M") == "1970-01-01 08:00"


def test_aware_timestamp_is_converted_for_display(monkeypatch) -> None:
    monkeypatch.setattr(config, "timezone_name", "Asia/Shanghai")
    value = datetime(2026, 8, 27, 2, 55, tzinfo=UTC)
    localized = localize_timestamp(value)
    assert localized.strftime("%Y-%m-%d %H:%M %z") == "2026-08-27 10:55 +0800"


def test_invalid_runtime_timezone_falls_back_to_utc(monkeypatch) -> None:
    monkeypatch.setattr(config, "timezone_name", "Invalid/Zone")
    assert format_epoch(0, "%Y-%m-%d %H:%M %z") == "1970-01-01 00:00 +0000"
