"""Tests for resilient Telegram polling requests."""

import asyncio
from pathlib import Path
import tomllib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

from ccgram.bot import create_bot
from ccgram.telegram_request import ResilientPollingHTTPXRequest


class TestResilientPollingHTTPXRequest:
    async def test_rebuilds_client_after_timeout(self) -> None:
        request = ResilientPollingHTTPXRequest()
        old_client = request._client

        with (
            patch.object(
                HTTPXRequest,
                "do_request",
                AsyncMock(side_effect=TimedOut("pool timeout")),
            ),
            pytest.raises(TimedOut),
        ):
            await request.do_request("https://example.com", "POST")

        assert request._client is not old_client
        assert old_client.is_closed
        assert not request._client.is_closed

    async def test_rebuilds_client_after_network_error(self) -> None:
        request = ResilientPollingHTTPXRequest()
        old_client = request._client

        with (
            patch.object(
                HTTPXRequest,
                "do_request",
                AsyncMock(side_effect=NetworkError("proxy broken")),
            ),
            pytest.raises(NetworkError),
        ):
            await request.do_request("https://example.com", "POST")

        assert request._client is not old_client
        assert old_client.is_closed
        assert not request._client.is_closed

    async def test_concurrent_failures_reset_shared_client_once(self) -> None:
        request = ResilientPollingHTTPXRequest()
        old_client = request._client
        both_entered = asyncio.Event()
        release = asyncio.Event()
        entered = 0

        async def fail_together(*_args, **_kwargs) -> None:
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
            raise TimedOut("shared client failed")

        with (
            patch.object(
                HTTPXRequest, "do_request", AsyncMock(side_effect=fail_together)
            ),
            patch.object(
                request, "_build_client", wraps=request._build_client
            ) as mock_build,
        ):
            calls = [
                asyncio.create_task(request.do_request("https://example.com", "POST"))
                for _ in range(2)
            ]
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            release.set()
            results = await asyncio.gather(*calls, return_exceptions=True)

        assert all(isinstance(result, TimedOut) for result in results)
        assert mock_build.call_count == 1
        assert request._client is not old_client
        assert old_client.is_closed


def _reset_log_calls(mock_logger, level: str) -> list:
    return [
        c
        for c in getattr(mock_logger, level).call_args_list
        if c.args and "Reset Telegram HTTP client" in c.args[0]
    ]


class TestResetWarningRateLimit:
    async def test_isolated_reset_logs_info(self) -> None:
        request = ResilientPollingHTTPXRequest()
        with (
            patch.object(
                HTTPXRequest,
                "do_request",
                AsyncMock(side_effect=TimedOut("t")),
            ),
            patch("ccgram.telegram_request.logger") as mock_logger,
            pytest.raises(TimedOut),
        ):
            await request.do_request("https://example.com", "POST")
        assert _reset_log_calls(mock_logger, "warning") == []
        assert len(_reset_log_calls(mock_logger, "info")) == 1

    async def test_sustained_outage_warns_once(self) -> None:
        request = ResilientPollingHTTPXRequest()
        with (
            patch.object(
                HTTPXRequest,
                "do_request",
                AsyncMock(side_effect=TimedOut("t")),
            ),
            patch("ccgram.telegram_request.logger") as mock_logger,
        ):
            for _ in range(5):
                with pytest.raises(TimedOut):
                    await request.do_request("https://example.com", "POST")

        assert len(_reset_log_calls(mock_logger, "warning")) == 1
        assert len(_reset_log_calls(mock_logger, "info")) == 4

    async def test_success_resets_consecutive_counter(self) -> None:
        request = ResilientPollingHTTPXRequest()
        response = (200, b'{"ok": true, "result": []}')
        mock = AsyncMock(
            side_effect=[TimedOut("t"), TimedOut("t"), response, TimedOut("t")]
        )

        with (
            patch.object(HTTPXRequest, "do_request", mock),
            patch("ccgram.telegram_request.logger") as mock_logger,
        ):
            for _ in range(2):
                with pytest.raises(TimedOut):
                    await request.post("u")
            await request.post("u")
            with pytest.raises(TimedOut):
                await request.post("u")

        assert _reset_log_calls(mock_logger, "warning") == []


class TestCreateBotPollingRequest:
    @patch("ccgram.bot.config")
    def test_uses_resilient_request_for_telegram_traffic(
        self, mock_config: MagicMock
    ) -> None:
        mock_config.telegram_bot_token = "fake:token"

        app = create_bot()

        assert isinstance(app.bot._request[0], ResilientPollingHTTPXRequest)
        assert isinstance(app.bot._request[1], ResilientPollingHTTPXRequest)
        assert app.bot._request[0]._client._transport._pool._max_connections == 1
        assert app.bot._request[1]._client._transport._pool._max_connections == 256
        assert app.bot._request[0].read_timeout == 20
        assert app.bot._request[1].read_timeout == 10
        assert app.bot._request[0].request_name == "getUpdates"
        assert app.bot._request[1].request_name == "Bot API"


class TestProjectDependencies:
    def test_declares_ptb_socks_support(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
        dependencies = pyproject["project"]["dependencies"]

        assert any(
            dependency.startswith("python-telegram-bot[")
            and "socks" in dependency.partition("[")[2].partition("]")[0].split(",")
            for dependency in dependencies
        )
