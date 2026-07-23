"""Unit tests for toolbar_callbacks dispatch + toolbar_keyboard label overrides.

Complements test_toolbar.py: covers the registered ``_dispatch`` guards,
herdr-style callback-data parsing, unknown-builtin / unsupported-type
handling, builtin screenshot/live delegation, the getfile success path,
and per-window label overrides in the keyboard builder.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.callback_data import (
    CB_LIVE_START,
    CB_STATUS_SCREENSHOT,
    CB_TOOLBAR,
)
from ccgram.handlers.toolbar.toolbar_callbacks import (
    _dispatch,
    _parse_callback_data,
    handle_toolbar_callback,
)
from ccgram.handlers.toolbar.toolbar_keyboard import (
    _clear_toolbar_labels,
    _set_action_label,
    build_toolbar_keyboard,
    reload_toolbar_config,
)
from ccgram.toolbar_config import (
    BUILTIN_ACTIONS,
    DEFAULT_LAYOUTS,
    ActionType,
    ButtonStyle,
    ToolbarAction,
    ToolbarConfig,
    ToolbarLayout,
)

_TC = "ccgram.handlers.toolbar.toolbar_callbacks"
_TK = "ccgram.handlers.toolbar.toolbar_keyboard"


@pytest.fixture(autouse=True)
def _fresh_toolbar_config():
    reload_toolbar_config()
    yield
    reload_toolbar_config()


def _make_query(data: str) -> AsyncMock:
    query = AsyncMock()
    query.data = data
    query.get_bot = MagicMock(return_value=MagicMock())
    return query


def _make_update(user_id: int = 100) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


# ── _parse_callback_data — herdr ids and edge shapes ─────────────────────


class TestParseCallbackDataShapes:
    def test_herdr_window_id_with_colons(self) -> None:
        # Action name is the substring after the LAST colon, so herdr tab
        # ids ("w2:t1") survive intact.
        assert _parse_callback_data("tb:w2:t1:mode") == ("w2:t1", "mode")

    def test_trailing_colon_yields_empty_action(self) -> None:
        assert _parse_callback_data("tb:@5:") == ("@5", "")

    def test_leading_colon_returns_none(self) -> None:
        # "tb::mode" → sep at index 0 → invalid (empty window_id).
        assert _parse_callback_data("tb::mode") is None


# ── Registered _dispatch entry point ─────────────────────────────────────


class TestRegisteredDispatch:
    async def test_no_callback_query_is_noop(self) -> None:
        update = _make_update()
        update.callback_query = None
        with patch(f"{_TC}.handle_toolbar_callback", new_callable=AsyncMock) as mock_h:
            await _dispatch(update, _make_context())
        mock_h.assert_not_awaited()

    async def test_no_user_is_noop(self) -> None:
        update = MagicMock()
        update.callback_query = _make_query("tb:@5:esc")
        update.effective_user = None
        with patch(f"{_TC}.handle_toolbar_callback", new_callable=AsyncMock) as mock_h:
            await _dispatch(update, _make_context())
        mock_h.assert_not_awaited()

    async def test_delegates_with_user_id_and_data(self) -> None:
        query = _make_query("tb:@5:esc")
        update = _make_update(user_id=777)
        update.callback_query = query
        context = _make_context()
        with patch(f"{_TC}.handle_toolbar_callback", new_callable=AsyncMock) as mock_h:
            await _dispatch(update, context)
        mock_h.assert_awaited_once_with(query, 777, "tb:@5:esc", update, context)


# ── Unknown builtin / unsupported action type ────────────────────────────


def _cfg_with(action: ToolbarAction) -> ToolbarConfig:
    return ToolbarConfig(
        layouts=dict(DEFAULT_LAYOUTS),
        actions={**BUILTIN_ACTIONS, action.name: action},
    )


class TestDispatchUnknowns:
    async def test_unknown_builtin_payload_alerts(self) -> None:
        action = ToolbarAction(
            name="mystery",
            emoji="❓",
            text="Myst",
            action_type="builtin",
            payload="nope",
        )
        query = _make_query("tb:@5:mystery")
        with (
            patch(f"{_TC}.get_toolbar_config", return_value=_cfg_with(action)),
            patch(f"{_TC}.user_owns_window", return_value=True),
        ):
            await handle_toolbar_callback(
                query, 100, "tb:@5:mystery", _make_update(), _make_context()
            )
        query.answer.assert_awaited_once_with("Unknown builtin: nope", show_alert=True)

    async def test_unsupported_action_type_alerts(self) -> None:
        action = ToolbarAction(
            name="weird",
            emoji="❓",
            text="Weird",
            action_type=cast(ActionType, "bogus"),
            payload="x",
        )
        query = _make_query("tb:@5:weird")
        with (
            patch(f"{_TC}.get_toolbar_config", return_value=_cfg_with(action)),
            patch(f"{_TC}.user_owns_window", return_value=True),
        ):
            await handle_toolbar_callback(
                query, 100, "tb:@5:weird", _make_update(), _make_context()
            )
        query.answer.assert_awaited_once_with(
            "Unsupported action type", show_alert=True
        )


# ── Builtin screenshot / live delegation ─────────────────────────────────


class TestBuiltinDelegation:
    async def test_screen_delegates_to_screenshot_handler(self) -> None:
        query = _make_query("tb:@5:screen")
        update = _make_update(user_id=100)
        context = _make_context()
        with (
            patch(f"{_TC}.user_owns_window", return_value=True),
            patch(
                "ccgram.handlers.live.screenshot_callbacks.handle_screenshot_callback",
                new_callable=AsyncMock,
            ) as mock_h,
        ):
            await handle_toolbar_callback(query, 100, "tb:@5:screen", update, context)
        mock_h.assert_awaited_once_with(
            query, 100, f"{CB_STATUS_SCREENSHOT}@5", update, context
        )

    async def test_live_delegates_with_live_start_data(self) -> None:
        query = _make_query("tb:@5:live")
        update = _make_update(user_id=100)
        context = _make_context()
        with (
            patch(f"{_TC}.user_owns_window", return_value=True),
            patch(
                "ccgram.handlers.live.screenshot_callbacks.handle_screenshot_callback",
                new_callable=AsyncMock,
            ) as mock_h,
        ):
            await handle_toolbar_callback(query, 100, "tb:@5:live", update, context)
        mock_h.assert_awaited_once_with(
            query, 100, f"{CB_LIVE_START}@5", update, context
        )

    async def test_screen_without_user_alerts(self) -> None:
        query = _make_query("tb:@5:screen")
        update = MagicMock()
        update.effective_user = None
        with patch(f"{_TC}.user_owns_window", return_value=True):
            await handle_toolbar_callback(
                query, 100, "tb:@5:screen", update, _make_context()
            )
        query.answer.assert_awaited_once_with("No user context", show_alert=True)


# ── Builtin getfile — success + state guard ──────────────────────────────


class TestBuiltinGetfile:
    async def test_success_opens_file_browser(self, tmp_path: Path) -> None:
        query = _make_query("tb:@5:getfile")
        update = _make_update(user_id=100)
        context = _make_context()
        with (
            patch(f"{_TC}.user_owns_window", return_value=True),
            patch(
                f"{_TC}.view_window",
                return_value=MagicMock(cwd=str(tmp_path)),
            ),
            patch(f"{_TC}.get_thread_id", return_value=42),
            patch(f"{_TC}.thread_router") as mock_router,
            patch(
                "ccgram.handlers.send.open_file_browser",
                new_callable=AsyncMock,
            ) as mock_open,
        ):
            mock_router.resolve_chat_id.return_value = -100
            await handle_toolbar_callback(query, 100, "tb:@5:getfile", update, context)
        mock_open.assert_awaited_once()
        args = mock_open.call_args.args
        assert args[1] == -100
        assert args[2] == 42
        assert args[4] == "@5"
        assert args[5] == tmp_path
        query.answer.assert_awaited_once_with()

    async def test_missing_user_data_alerts_state_error(self, tmp_path: Path) -> None:
        query = _make_query("tb:@5:getfile")
        update = _make_update(user_id=100)
        context = MagicMock()
        context.user_data = None
        with (
            patch(f"{_TC}.user_owns_window", return_value=True),
            patch(
                f"{_TC}.view_window",
                return_value=MagicMock(cwd=str(tmp_path)),
            ),
        ):
            await handle_toolbar_callback(query, 100, "tb:@5:getfile", update, context)
        query.answer.assert_awaited_once_with("State error", show_alert=True)


# ── Keyboard builder — per-window label overrides ────────────────────────


class TestKeyboardLabelOverrides:
    def _find_button(
        self,
        window_id: str,
        action_name: str,
        style: ButtonStyle = "emoji_text",
    ):
        layout = ToolbarLayout(style=style, buttons=((action_name,),))
        cfg = ToolbarConfig(layouts={"claude": layout}, actions=dict(BUILTIN_ACTIONS))
        with patch(f"{_TK}.get_toolbar_config", return_value=cfg):
            kb = build_toolbar_keyboard(window_id, "claude")
        return kb.inline_keyboard[0][0]

    def test_override_with_emoji_text_style_keeps_emoji(self) -> None:
        _clear_toolbar_labels("@71")
        _set_action_label("@71", "mode", "Plan")
        btn = self._find_button("@71", "mode", style="emoji_text")
        assert btn.text == "\U0001f500 Plan"

    def test_override_with_text_style_is_label_only(self) -> None:
        _clear_toolbar_labels("@72")
        _set_action_label("@72", "mode", "Edit")
        btn = self._find_button("@72", "mode", style="text")
        assert btn.text == "Edit"

    def test_override_scoped_per_window(self) -> None:
        _clear_toolbar_labels("@73")
        _clear_toolbar_labels("@74")
        _set_action_label("@73", "mode", "Plan")
        btn_other = self._find_button("@74", "mode")
        assert btn_other.text == "\U0001f500 Mode"

    def test_cleared_override_reverts_to_default_render(self) -> None:
        _set_action_label("@75", "mode", "Full")
        _clear_toolbar_labels("@75")
        btn = self._find_button("@75", "mode")
        assert btn.text == "\U0001f500 Mode"

    def test_callback_data_fits_telegram_limit(self) -> None:
        kb = build_toolbar_keyboard("@1234567890", "claude")
        for row in kb.inline_keyboard:
            for btn in row:
                assert isinstance(btn.callback_data, str)
                assert btn.callback_data.startswith(CB_TOOLBAR)
                assert len(btn.callback_data.encode()) <= 64
