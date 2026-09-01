"""Central handler registration for the Telegram bot Application.

Owns the command/message/callback/inline handler registration that used
to live inline in ``bot.py``. ``register_all()`` is the single entry
point; ``bot.py`` is a factory + lifecycle hooks only.

Every handler called below lives in a feature subpackage under
``handlers/`` — this module only assembles them in the order PTB
requires.
"""

from dataclasses import dataclass
from typing import TypeAlias

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from telegram.ext._utils.types import HandlerCallback

from ..access_control import Role, require_role
from .callback_registry import dispatch as _dispatch_callback
from .callback_registry import load_handlers as _load_callback_handlers
from .agent_command import agent_command
from .lifecycle_commands import (
    autoname_command,
    diag_command,
    handoff_command,
    ops_command,
    park_command,
    replay_command,
    wake_command,
)
from .cleanup import unbind_command
from .command_history import recall_command
from .commands import (
    commands_command,
    forward_command_handler,
    toolbar_command,
)
from .file_handler import handle_document_message, handle_photo_message
from .inline import inline_query_handler, unsupported_content_handler
from .live import live_command, panes_command, screenshot_command
from .messaging_pipeline import toolcalls_command, verbose_command
from .diff_command import diff_command
from .last_reply import last_command
from .lane_command import lane_command
from .usage_command import usage_command
from .recovery import restore_command, resume_command
from .recovery.history import history_command
from .search_command import search_command
from .selftest_command import selftest_command
from .send import send_command
from .sessions_dashboard import sessions_command
from .split_command import split_command
from .sync_command import sync_command
from .task_commands import (
    task_add_command,
    task_cancel_all_command,
    task_cancel_command,
    task_force_cancel_command,
    task_new_command,
    task_retry_command,
    tasks_command,
)
from .text.text_handler import text_handler
from .topics import new_command
from .topics.topic_lifecycle import topic_closed_handler, topic_edited_handler
from .upgrade import upgrade_command
from .voice import handle_voice_message

HandlerFn: TypeAlias = HandlerCallback


@dataclass(frozen=True)
class CommandSpec:
    """Specification for a single PTB CommandHandler registration."""

    name: str
    handler: HandlerFn
    minimum_role: Role | None = "operator"


def register_all(
    application: Application,
    group_filter: filters.BaseFilter,
) -> None:
    """Register every command, callback, message and inline-query handler.

    Order is significant: PTB dispatches the first matching handler, so
    explicit CommandHandlers must precede the COMMAND-fallback
    MessageHandler, which must precede the TEXT MessageHandler.
    """
    command_specs: list[CommandSpec] = [
        # /start already performs the legacy allow-list check itself and is a
        # read-only welcome/reset action. Keep its callback identity stable.
        CommandSpec("start", new_command, None),
        CommandSpec("history", history_command, "viewer"),
        CommandSpec("commands", commands_command, "viewer"),
        CommandSpec("sessions", sessions_command, "viewer"),
        CommandSpec("resume", resume_command),
        CommandSpec("unbind", unbind_command, "admin"),
        CommandSpec("upgrade", upgrade_command, "admin"),
        CommandSpec("recall", recall_command, "viewer"),
        CommandSpec("screenshot", screenshot_command, "viewer"),
        CommandSpec("live", live_command, "viewer"),
        CommandSpec("panes", panes_command, "viewer"),
        CommandSpec("split", split_command),
        CommandSpec("sync", sync_command, "admin"),
        CommandSpec("toolbar", toolbar_command),
        CommandSpec("send", send_command),
        CommandSpec("verbose", verbose_command),
        CommandSpec("toolcalls", toolcalls_command),
        CommandSpec("restore", restore_command),
        CommandSpec("last", last_command, "viewer"),
        CommandSpec("diff", diff_command, "viewer"),
        CommandSpec("usage", usage_command, "viewer"),
        CommandSpec("search", search_command, "viewer"),
        CommandSpec("agent", agent_command),
        CommandSpec("provider", agent_command),  # alias
        CommandSpec("handoff", handoff_command),
        CommandSpec("autoname", autoname_command),
        CommandSpec("park", park_command),
        CommandSpec("wake", wake_command),
        CommandSpec("diag", diag_command, "viewer"),
        CommandSpec("replay", replay_command, "viewer"),
        CommandSpec("ops", ops_command, "viewer"),
        CommandSpec("selftest", selftest_command, "viewer"),
        CommandSpec("tasks", tasks_command, "viewer"),
        CommandSpec("task_add", task_add_command),
        CommandSpec("task_new", task_new_command),
        CommandSpec("task_cancel", task_cancel_command),
        CommandSpec("task_retry", task_retry_command),
        CommandSpec("task_cancel_all", task_cancel_all_command, "admin"),
        CommandSpec("task_force_cancel", task_force_cancel_command, "admin"),
        CommandSpec("lane", lane_command, "viewer"),
    ]

    for spec in command_specs:
        callback = (
            require_role(spec.minimum_role)(spec.handler)
            if spec.minimum_role is not None
            else spec.handler
        )
        application.add_handler(
            CommandHandler(
                spec.name,
                callback,
                filters=group_filter,
            )
        )

    _load_callback_handlers()
    application.add_handler(
        CallbackQueryHandler(require_role("operator")(_dispatch_callback))
    )

    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED & group_filter,
            topic_closed_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED & group_filter,
            topic_edited_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.COMMAND & group_filter,
            require_role("operator")(forward_command_handler),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & group_filter,
            require_role("operator")(text_handler),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.PHOTO & group_filter,
            require_role("operator")(handle_photo_message),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Document.ALL & group_filter,
            require_role("operator")(handle_document_message),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.VOICE & group_filter,
            require_role("operator")(handle_voice_message),
        )
    )
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND
            & ~filters.TEXT
            & ~filters.PHOTO
            & ~filters.Document.ALL
            & ~filters.VOICE
            & ~filters.StatusUpdate.ALL
            & group_filter,
            unsupported_content_handler,
        )
    )

    application.add_handler(InlineQueryHandler(inline_query_handler))


COMMAND_NAMES: tuple[str, ...] = (
    "start",
    "history",
    "commands",
    "sessions",
    "resume",
    "unbind",
    "upgrade",
    "recall",
    "screenshot",
    "live",
    "panes",
    "split",
    "sync",
    "toolbar",
    "send",
    "verbose",
    "toolcalls",
    "restore",
    "last",
    "diff",
    "usage",
    "search",
    "agent",
    "provider",
    "handoff",
    "autoname",
    "park",
    "wake",
    "diag",
    "replay",
    "ops",
    "selftest",
    "tasks",
    "task_add",
    "task_new",
    "task_cancel",
    "task_retry",
    "task_cancel_all",
    "task_force_cancel",
    "lane",
)
"""Sentinel for tests: the exact command names register_all installs, in order."""
