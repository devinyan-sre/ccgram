from types import SimpleNamespace

from ccgram.config import config
from ccgram.handlers.group_activation import evaluate_group_activation


def _message(*, text_reply: bool = False):
    reply = (
        SimpleNamespace(from_user=SimpleNamespace(id=999)) if text_reply else None
    )
    return SimpleNamespace(
        chat=SimpleNamespace(type="supergroup"),
        reply_to_message=reply,
    )


def test_unaddressed_group_media_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(config, "require_mention_in_groups", True)
    client = SimpleNamespace(id=999, username="ops_bot")

    activation = evaluate_group_activation(_message(), client, "look at this")

    assert activation.accepted is False


def test_media_caption_mention_is_case_insensitive_and_stripped(monkeypatch) -> None:
    monkeypatch.setattr(config, "require_mention_in_groups", True)
    client = SimpleNamespace(id=999, username="ops_bot")

    activation = evaluate_group_activation(
        _message(), client, "@OPS_BOT 看一下这张图"
    )

    assert activation.accepted is True
    assert activation.text == "看一下这张图"


def test_captionless_media_can_activate_by_replying_to_bot(monkeypatch) -> None:
    monkeypatch.setattr(config, "require_mention_in_groups", True)
    client = SimpleNamespace(id=999, username="ops_bot")

    activation = evaluate_group_activation(_message(text_reply=True), client, "")

    assert activation.accepted is True
    assert activation.text == ""
