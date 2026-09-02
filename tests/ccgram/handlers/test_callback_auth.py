"""Tests for callback handler authorization checks."""

from unittest.mock import patch

from ccgram.handlers.callback_helpers import user_owns_window


class TestUserOwnsWindow:
    def test_owns_bound_window(self) -> None:
        with patch("ccgram.handlers.callback_helpers.thread_router") as mock_sm:
            mock_sm.user_owns_window.side_effect = lambda _uid, window_id: (
                window_id in {"@0", "@5"}
            )
            assert user_owns_window(100, "@0")
            assert user_owns_window(100, "@5")

    def test_does_not_own_unbound_window(self) -> None:
        with patch("ccgram.handlers.callback_helpers.thread_router") as mock_sm:
            mock_sm.user_owns_window.return_value = False
            assert not user_owns_window(100, "@99")

    def test_no_bindings(self) -> None:
        with patch("ccgram.handlers.callback_helpers.thread_router") as mock_sm:
            mock_sm.user_owns_window.return_value = False
            assert not user_owns_window(100, "@0")

    def test_different_user_does_not_own(self) -> None:
        with patch("ccgram.handlers.callback_helpers.thread_router") as mock_sm:
            mock_sm.user_owns_window.side_effect = lambda uid, window_id: (
                uid == 100 and window_id in {"@0", "@5"}
            )
            assert user_owns_window(100, "@0")
            assert not user_owns_window(200, "@0")
