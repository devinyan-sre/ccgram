from unittest.mock import patch

from ccgram.access_control import has_role, role_for


def test_role_ordering() -> None:
    with patch("ccgram.access_control.config.user_role", return_value="operator"):
        assert role_for(10) == "operator"
        assert has_role(10, "viewer") is True
        assert has_role(10, "operator") is True
        assert has_role(10, "admin") is False
