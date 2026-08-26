"""Role-based authorization layered on top of the Telegram allow-list."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, Literal, TypeAlias, cast

from telegram import Update

from .config import config

Role: TypeAlias = Literal["viewer", "operator", "admin"]
_RANK: dict[Role, int] = {"viewer": 0, "operator": 1, "admin": 2}


def role_for(user_id: int) -> Role | None:
    role = config.user_role(user_id)
    return cast(Role | None, role)


def has_role(user_id: int, minimum: Role) -> bool:
    role = role_for(user_id)
    return role is not None and _RANK[role] >= _RANK[minimum]


def require_role(
    minimum: Role,
) -> Callable[
    [Callable[..., Coroutine[Any, Any, Any]]],
    Callable[..., Coroutine[Any, Any, Any]],
]:
    """Guard a PTB handler and fail silently for unauthorized group users."""

    def decorate(
        handler: Callable[..., Coroutine[Any, Any, Any]],
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        @wraps(handler)
        async def guarded(update: Update, *args: Any, **kwargs: Any) -> Any:
            user = update.effective_user
            if user is None or not has_role(user.id, minimum):
                message = update.effective_message
                if message is not None and message.chat.type == "private":
                    await message.reply_text(
                        f"❌ This action requires the {minimum} role."
                    )
                return None
            return await handler(update, *args, **kwargs)

        return guarded

    return decorate


__all__ = ["Role", "has_role", "require_role", "role_for"]
