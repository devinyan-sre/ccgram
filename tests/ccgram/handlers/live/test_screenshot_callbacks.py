"""Unit tests for screenshot_callbacks — dispatch routing, guards, capture flows.

Live-start/live-stop handler details are covered in test_live_view.py;
/live command flows in test_screenshot_commands.py. This file covers the
dispatcher, refresh / status-screenshot / pane-screenshot flows, and the
quick-key map contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TelegramError

from ccgram.handlers.callback_data import (
    CB_KEYS_PREFIX,
    CB_LIVE_START,
    CB_LIVE_STOP,
    CB_PANE_SCREENSHOT,
    CB_SCREENSHOT_REFRESH,
    CB_STATUS_SCREENSHOT,
)
from ccgram.handlers.live.screenshot_callbacks import (
    KEY_LABELS,
    KEYS_SEND_MAP,
    _handle_pane_screenshot,
    _handle_refresh,
    _handle_status_screenshot,
    build_screenshot_keyboard,
    handle_screenshot_callback,
)

_SC = "ccgram.handlers.live.screenshot_callbacks"


def _make_query() -> tuple[AsyncMock, MagicMock]:
    query = AsyncMock()
    query.message = MagicMock(message_id=200)
    query.get_bot = MagicMock(return_value=MagicMock())
    update = MagicMock()
    return query, update


# ── Dispatch routing ─────────────────────────────────────────────────────


class TestDispatchRouting:
    async def _dispatch(self, data: str) -> dict[str, AsyncMock]:
        query, update = _make_query()
        mocks = {
            "_handle_live_start": AsyncMock(),
            "_handle_live_stop": AsyncMock(),
            "_handle_status_screenshot": AsyncMock(),
            "_handle_pane_screenshot": AsyncMock(),
            "_handle_refresh": AsyncMock(),
        }
        with (
            patch(f"{_SC}._handle_live_start", mocks["_handle_live_start"]),
            patch(f"{_SC}._handle_live_stop", mocks["_handle_live_stop"]),
            patch(
                f"{_SC}._handle_status_screenshot",
                mocks["_handle_status_screenshot"],
            ),
            patch(
                f"{_SC}._handle_pane_screenshot",
                mocks["_handle_pane_screenshot"],
            ),
            patch(f"{_SC}._handle_refresh", mocks["_handle_refresh"]),
        ):
            await handle_screenshot_callback(query, 1, data, update, MagicMock())
        mocks["query"] = query
        return mocks

    async def test_routes_live_start(self) -> None:
        mocks = await self._dispatch(f"{CB_LIVE_START}@0")
        mocks["_handle_live_start"].assert_awaited_once()
        mocks["_handle_refresh"].assert_not_awaited()

    async def test_routes_live_stop(self) -> None:
        mocks = await self._dispatch(f"{CB_LIVE_STOP}@0")
        mocks["_handle_live_stop"].assert_awaited_once()
        mocks["_handle_live_start"].assert_not_awaited()

    async def test_routes_status_screenshot(self) -> None:
        mocks = await self._dispatch(f"{CB_STATUS_SCREENSHOT}@0")
        mocks["_handle_status_screenshot"].assert_awaited_once()

    async def test_routes_pane_screenshot(self) -> None:
        mocks = await self._dispatch(f"{CB_PANE_SCREENSHOT}@0|%3")
        mocks["_handle_pane_screenshot"].assert_awaited_once()

    async def test_routes_refresh_without_update(self) -> None:
        mocks = await self._dispatch(f"{CB_SCREENSHOT_REFRESH}@0")
        mocks["_handle_refresh"].assert_awaited_once()
        # Refresh handler takes (query, user_id, data) — no update arg.
        args = mocks["_handle_refresh"].call_args.args
        assert len(args) == 3
        assert args[2] == f"{CB_SCREENSHOT_REFRESH}@0"

    async def test_unknown_prefix_is_noop(self) -> None:
        mocks = await self._dispatch("zz:unknown:@0")
        for name, mock in mocks.items():
            if name.startswith("_handle_"):
                mock.assert_not_awaited()
        mocks["query"].answer.assert_not_awaited()


# ── Refresh flow ─────────────────────────────────────────────────────────


class TestHandleRefresh:
    async def test_rejects_non_owner(self) -> None:
        query, _ = _make_query()
        with patch(f"{_SC}.user_owns_window", return_value=False):
            await _handle_refresh(query, 1, f"{CB_SCREENSHOT_REFRESH}@0")
        query.answer.assert_awaited_once_with("Not your session", show_alert=True)

    async def test_dead_window_alerts(self) -> None:
        query, _ = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await _handle_refresh(query, 1, f"{CB_SCREENSHOT_REFRESH}@0")
        query.answer.assert_awaited_once_with(
            "Window no longer exists", show_alert=True
        )

    async def test_capture_failure_alerts(self) -> None:
        query, _ = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane = AsyncMock(return_value=None)
            await _handle_refresh(query, 1, f"{CB_SCREENSHOT_REFRESH}@0")
        query.answer.assert_awaited_once_with("Failed to capture pane", show_alert=True)

    async def test_success_edits_media(self) -> None:
        query, _ = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane = AsyncMock(return_value="terminal text")
            await _handle_refresh(query, 1, f"{CB_SCREENSHOT_REFRESH}@0")
        query.edit_message_media.assert_awaited_once()
        query.answer.assert_awaited_once_with("Refreshed")

    async def test_edit_failure_alerts(self) -> None:
        query, _ = _make_query()
        query.edit_message_media = AsyncMock(side_effect=TelegramError("boom"))
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane = AsyncMock(return_value="terminal text")
            await _handle_refresh(query, 1, f"{CB_SCREENSHOT_REFRESH}@0")
        query.answer.assert_awaited_once_with("Failed to refresh", show_alert=True)

    async def test_pane_target_uses_capture_pane_by_id(self) -> None:
        query, _ = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane_by_id = AsyncMock(return_value="pane text")
            await _handle_refresh(query, 1, f"{CB_SCREENSHOT_REFRESH}@0|%3")
        mock_tmux.capture_pane_by_id.assert_awaited_once_with(
            "%3", with_ansi=True, window_id="@0"
        )
        query.edit_message_media.assert_awaited_once()


# ── Status screenshot flow ───────────────────────────────────────────────


class TestHandleStatusScreenshot:
    async def test_rejects_non_owner(self) -> None:
        query, update = _make_query()
        with patch(f"{_SC}.user_owns_window", return_value=False):
            await _handle_status_screenshot(
                query, 1, f"{CB_STATUS_SCREENSHOT}@0", update
            )
        query.answer.assert_awaited_once_with("Not your session", show_alert=True)

    async def test_window_not_found_alerts(self) -> None:
        query, update = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await _handle_status_screenshot(
                query, 1, f"{CB_STATUS_SCREENSHOT}@0", update
            )
        query.answer.assert_awaited_once_with("Window not found", show_alert=True)

    async def test_capture_failure_alerts(self) -> None:
        query, update = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane = AsyncMock(return_value=None)
            await _handle_status_screenshot(
                query, 1, f"{CB_STATUS_SCREENSHOT}@0", update
            )
        query.answer.assert_awaited_once_with("Failed to capture", show_alert=True)

    async def test_no_thread_alerts(self) -> None:
        query, update = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
            patch(f"{_SC}.get_thread_id", return_value=None),
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane = AsyncMock(return_value="text")
            await _handle_status_screenshot(
                query, 1, f"{CB_STATUS_SCREENSHOT}@0", update
            )
        query.answer.assert_awaited_once_with("Use in a topic", show_alert=True)

    async def test_success_sends_document(self) -> None:
        query, update = _make_query()
        mock_client = MagicMock()
        mock_client.send_document = AsyncMock()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
            patch(f"{_SC}.get_thread_id", return_value=42),
            patch(f"{_SC}.thread_router") as mock_router,
            patch(f"{_SC}.PTBTelegramClient", return_value=mock_client),
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane = AsyncMock(return_value="text")
            mock_router.resolve_chat_id.return_value = -100
            await _handle_status_screenshot(
                query, 1, f"{CB_STATUS_SCREENSHOT}@0", update
            )
        mock_client.send_document.assert_awaited_once()
        kwargs = mock_client.send_document.call_args.kwargs
        assert kwargs["chat_id"] == -100
        assert kwargs["message_thread_id"] == 42
        assert kwargs["filename"] == "screenshot.png"
        query.answer.assert_awaited_once_with("\U0001f4f8")

    async def test_send_failure_alerts(self) -> None:
        query, update = _make_query()
        mock_client = MagicMock()
        mock_client.send_document = AsyncMock(side_effect=TelegramError("denied"))
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
            patch(f"{_SC}.get_thread_id", return_value=42),
            patch(f"{_SC}.thread_router") as mock_router,
            patch(f"{_SC}.PTBTelegramClient", return_value=mock_client),
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.capture_pane = AsyncMock(return_value="text")
            mock_router.resolve_chat_id.return_value = -100
            await _handle_status_screenshot(
                query, 1, f"{CB_STATUS_SCREENSHOT}@0", update
            )
        query.answer.assert_awaited_once_with(
            "Failed to send screenshot", show_alert=True
        )


# ── Pane screenshot flow ─────────────────────────────────────────────────


class TestHandlePaneScreenshot:
    async def test_missing_delimiter_answers_invalid(self) -> None:
        query, update = _make_query()
        await _handle_pane_screenshot(query, 1, f"{CB_PANE_SCREENSHOT}@0", update)
        query.answer.assert_awaited_once_with("Invalid data")

    async def test_rejects_non_owner(self) -> None:
        query, update = _make_query()
        with patch(f"{_SC}.user_owns_window", return_value=False):
            await _handle_pane_screenshot(
                query, 1, f"{CB_PANE_SCREENSHOT}@0|%3", update
            )
        query.answer.assert_awaited_once_with("Not your session", show_alert=True)

    async def test_capture_failure_alerts(self) -> None:
        query, update = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
        ):
            mock_tmux.capture_pane_by_id = AsyncMock(return_value=None)
            await _handle_pane_screenshot(
                query, 1, f"{CB_PANE_SCREENSHOT}@0|%3", update
            )
        query.answer.assert_awaited_once_with("Failed to capture pane", show_alert=True)

    async def test_success_sends_pane_document(self) -> None:
        query, update = _make_query()
        mock_client = MagicMock()
        mock_client.send_document = AsyncMock()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
            patch(f"{_SC}.get_thread_id", return_value=42),
            patch(f"{_SC}.thread_router") as mock_router,
            patch(f"{_SC}.PTBTelegramClient", return_value=mock_client),
        ):
            mock_tmux.capture_pane_by_id = AsyncMock(return_value="pane text")
            mock_router.resolve_chat_id.return_value = -100
            await _handle_pane_screenshot(
                query, 1, f"{CB_PANE_SCREENSHOT}@0|%3", update
            )
        mock_tmux.capture_pane_by_id.assert_awaited_once_with(
            "%3", with_ansi=True, window_id="@0"
        )
        kwargs = mock_client.send_document.call_args.kwargs
        assert kwargs["filename"] == "pane_%3.png"
        query.answer.assert_awaited_once_with("\U0001f4f8 Pane %3")

    async def test_herdr_ids_round_trip(self) -> None:
        """Colon-bearing herdr ids split on the pipe delimiter, not the colon."""
        query, update = _make_query()
        with (
            patch(f"{_SC}.user_owns_window", return_value=True) as mock_owns,
            patch(f"{_SC}.tmux_manager") as mock_tmux,
        ):
            mock_tmux.capture_pane_by_id = AsyncMock(return_value=None)
            await _handle_pane_screenshot(
                query, 1, f"{CB_PANE_SCREENSHOT}w2:t1|w2:p1", update
            )
        mock_owns.assert_called_once_with(1, "w2:t1")
        mock_tmux.capture_pane_by_id.assert_awaited_once_with(
            "w2:p1", with_ansi=True, window_id="w2:t1"
        )

    async def test_send_failure_alerts(self) -> None:
        query, update = _make_query()
        mock_client = MagicMock()
        mock_client.send_document = AsyncMock(side_effect=TelegramError("denied"))
        with (
            patch(f"{_SC}.user_owns_window", return_value=True),
            patch(f"{_SC}.tmux_manager") as mock_tmux,
            patch(
                f"{_SC}.text_to_image",
                new_callable=AsyncMock,
                return_value=b"PNG",
            ),
            patch(f"{_SC}.get_thread_id", return_value=42),
            patch(f"{_SC}.thread_router") as mock_router,
            patch(f"{_SC}.PTBTelegramClient", return_value=mock_client),
        ):
            mock_tmux.capture_pane_by_id = AsyncMock(return_value="pane text")
            mock_router.resolve_chat_id.return_value = -100
            await _handle_pane_screenshot(
                query, 1, f"{CB_PANE_SCREENSHOT}@0|%3", update
            )
        query.answer.assert_awaited_once_with(
            "Failed to send screenshot", show_alert=True
        )


# ── Quick-key map contract ───────────────────────────────────────────────


class TestQuickKeyMaps:
    def test_key_maps_have_same_ids(self) -> None:
        assert set(KEYS_SEND_MAP) == set(KEY_LABELS)

    def test_keyboard_quick_keys_reference_known_ids(self) -> None:
        kb = build_screenshot_keyboard("@0")
        flat = [btn for row in kb.inline_keyboard for btn in row]
        key_btns = [
            btn
            for btn in flat
            if isinstance(btn.callback_data, str)
            and btn.callback_data.startswith(CB_KEYS_PREFIX)
        ]
        assert key_btns, "screenshot keyboard should contain quick-key buttons"
        for btn in key_btns:
            assert isinstance(btn.callback_data, str)
            key_id = btn.callback_data[len(CB_KEYS_PREFIX) :].split(":", 1)[0]
            assert key_id in KEYS_SEND_MAP

    def test_callback_data_fits_telegram_limit(self) -> None:
        kb = build_screenshot_keyboard("@12345", pane_id="%67890")
        for row in kb.inline_keyboard:
            for btn in row:
                assert isinstance(btn.callback_data, str)
                assert len(btn.callback_data.encode()) <= 64
