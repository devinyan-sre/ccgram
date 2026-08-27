"""Shared group activation policy for every inbound content type."""

from __future__ import annotations

from dataclasses import dataclass

from telegram import Message

from ..config import config
from ..telegram_client import TelegramClient


@dataclass(frozen=True, slots=True)
class GroupActivation:
    accepted: bool
    text: str


def evaluate_group_activation(
    message: Message,
    client: TelegramClient,
    text: str,
) -> GroupActivation:
    """Apply the common @mention/reply gate and strip the bot mention."""
    if not config.require_mention_in_groups or message.chat.type not in (
        "group",
        "supergroup",
    ):
        return GroupActivation(True, text)

    username = (client.username or "").strip()
    mention = f"@{username}" if username else ""
    reply = message.reply_to_message
    replies_to_bot = bool(
        reply and reply.from_user and reply.from_user.id == client.id
    )
    contains_mention = bool(mention and mention.lower() in text.lower())
    if not contains_mention and not replies_to_bot:
        return GroupActivation(False, text)
    if not contains_mention:
        return GroupActivation(True, text)

    start = text.lower().find(mention.lower())
    cleaned = (text[:start] + text[start + len(mention) :]).strip()
    return GroupActivation(True, cleaned)


__all__ = ["GroupActivation", "evaluate_group_activation"]
