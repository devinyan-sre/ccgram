from unittest.mock import patch

from ccgram import task_focus


def test_focus_is_one_shot() -> None:
    task_focus.clear_for_testing()
    task_focus.select(-100, 7, 10, "t0042")
    assert task_focus.consume(-100, 7, 10) == "T0042"
    assert task_focus.consume(-100, 7, 10) is None


def test_expired_focus_fails_closed() -> None:
    task_focus.clear_for_testing()
    with patch("ccgram.task_focus.time.monotonic", return_value=100.0):
        task_focus.select(-100, 7, 10, "T0042")
    with patch("ccgram.task_focus.time.monotonic", return_value=1000.0):
        assert task_focus.consume(-100, 7, 10) is None
