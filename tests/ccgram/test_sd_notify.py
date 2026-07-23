"""Tests for sd_notify — systemd readiness + health-gated watchdog heartbeat."""

import asyncio
import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from ccgram import sd_notify


@pytest.fixture(autouse=True)
def _clean_watchdog(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    yield
    sd_notify.stop_watchdog()


@pytest.fixture
def notify_socket(tmp_path: Path, monkeypatch) -> Iterator[socket.socket]:
    """A bound unix datagram socket exported as $NOTIFY_SOCKET."""
    path = tmp_path / "notify.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.settimeout(2.0)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    yield sock
    sock.close()


class TestNotify:
    def test_noop_without_socket_env(self) -> None:
        assert sd_notify.notify("READY=1") is False

    def test_sends_datagram(self, notify_socket: socket.socket) -> None:
        assert sd_notify.notify("READY=1") is True
        assert notify_socket.recv(64) == b"READY=1"

    def test_send_failure_returns_false(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone.sock"))
        assert sd_notify.notify("READY=1") is False


class TestWatchdogInterval:
    def test_none_without_env(self) -> None:
        assert sd_notify.watchdog_interval() is None

    def test_parses_usec(self, monkeypatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "90000000")
        assert sd_notify.watchdog_interval() == pytest.approx(90.0)

    def test_respects_foreign_watchdog_pid(self, monkeypatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "90000000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
        assert sd_notify.watchdog_interval() is None

    def test_own_pid_accepted(self, monkeypatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "90000000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
        assert sd_notify.watchdog_interval() == pytest.approx(90.0)

    def test_invalid_usec(self, monkeypatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "banana")
        assert sd_notify.watchdog_interval() is None


class TestStartWatchdog:
    async def test_none_when_unarmed(self) -> None:
        assert sd_notify.start_watchdog(lambda: True) is None

    async def test_pings_while_healthy(
        self, monkeypatch, notify_socket: socket.socket
    ) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "200000")  # 0.2s → ping every 0.1s
        task = sd_notify.start_watchdog(lambda: True)
        assert task is not None
        try:
            data = await asyncio.to_thread(notify_socket.recv, 64)
            assert data == b"WATCHDOG=1"
        finally:
            sd_notify.stop_watchdog()

    async def test_skips_ping_when_unhealthy(
        self, monkeypatch, notify_socket: socket.socket
    ) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "200000")
        notify_socket.settimeout(0.5)
        task = sd_notify.start_watchdog(lambda: False)
        assert task is not None
        try:
            with pytest.raises(TimeoutError):
                await asyncio.to_thread(notify_socket.recv, 64)
        finally:
            sd_notify.stop_watchdog()

    async def test_idempotent_start(
        self, monkeypatch, notify_socket: socket.socket
    ) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "200000")
        task1 = sd_notify.start_watchdog(lambda: True)
        task2 = sd_notify.start_watchdog(lambda: True)
        assert task1 is task2
        sd_notify.stop_watchdog()
