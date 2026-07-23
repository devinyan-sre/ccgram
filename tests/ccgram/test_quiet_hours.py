"""Tests for the quiet-hours window logic."""

import datetime as dt
from unittest.mock import patch

from ccgram.quiet_hours import is_quiet, parse_spec, silent_kwargs


class TestParseSpec:
    def test_empty_disabled(self) -> None:
        assert parse_spec("") is None
        assert parse_spec("   ") is None

    def test_valid_window(self) -> None:
        assert parse_spec("23:00-08:00") == (dt.time(23, 0), dt.time(8, 0))

    def test_whitespace_tolerated(self) -> None:
        assert parse_spec(" 22:30 - 07:15 ") == (dt.time(22, 30), dt.time(7, 15))

    def test_invalid_shapes_disabled(self) -> None:
        assert parse_spec("23:00") is None
        assert parse_spec("25:00-08:00") is None
        assert parse_spec("a-b") is None

    def test_zero_length_window_disabled(self) -> None:
        assert parse_spec("08:00-08:00") is None


class TestIsQuiet:
    def test_none_window_never_quiet(self) -> None:
        assert not is_quiet(None, dt.time(3, 0))

    def test_same_day_window(self) -> None:
        window = (dt.time(13, 0), dt.time(14, 0))
        assert is_quiet(window, dt.time(13, 30))
        assert not is_quiet(window, dt.time(14, 0))
        assert not is_quiet(window, dt.time(12, 59))

    def test_midnight_wrap(self) -> None:
        window = (dt.time(23, 0), dt.time(8, 0))
        assert is_quiet(window, dt.time(23, 30))
        assert is_quiet(window, dt.time(3, 0))
        assert not is_quiet(window, dt.time(8, 0))
        assert not is_quiet(window, dt.time(12, 0))


class TestSilentKwargs:
    def test_disabled_returns_empty(self) -> None:
        with patch("ccgram.config.config") as mock_config:
            mock_config.quiet_hours = ""
            assert silent_kwargs() == {}

    def test_inside_window_silences(self) -> None:
        with patch("ccgram.config.config") as mock_config:
            mock_config.quiet_hours = "00:00-23:59"
            assert silent_kwargs() == {"disable_notification": True}
