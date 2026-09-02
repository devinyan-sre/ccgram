"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import asyncio
import contextlib
import re
import time

import structlog

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

from ...i18n import t
from ...providers import get_provider_for_window
from ...telegram_client import TelegramClient
from ...window_query import get_window_provider
from ...thread_router import thread_router
from ...multiplexer import multiplexer as tmux_manager
from ...topic_state_registry import topic_state
from ..callback_data import (
    CB_ASK_CHOICE,
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from ..messaging_pipeline.message_sender import (
    NO_LINK_PREVIEW,
    is_thread_gone,
    rate_limit_send,
)

logger = structlog.get_logger()

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset(
    {
        "AskUserQuestion",
        "ExitPlanMode",
        # Codex native tool name before normalization/fallback.
        "request_user_input",
    }
)

# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}

# Track interactive mode: (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}

# Direct-choice callbacks must belong to the exact rendered Telegram prompt.
# Sequences invalidate delayed or double-tapped buttons after one selection.
_interactive_contexts: dict[tuple[int, int], tuple[int, int]] = {}
_interactive_sequences: dict[tuple[int, int], int] = {}
_interactive_contents: dict[tuple[int, int], str] = {}

# Cooldown to prevent flood when interactive sends fail repeatedly
_send_cooldowns: dict[tuple[int, int], float] = {}
_SEND_RETRY_INTERVAL = 5.0  # seconds between retries for failed sends
_DEAD_TOPIC_RETRY_INTERVAL = 60.0  # longer backoff when topic is deleted

# Single in-call retry on transient transport errors when sending interactive UI.
_INTERACTIVE_SEND_RETRIES = 1
_INTERACTIVE_SEND_RETRY_BACKOFF_S = 1.0

# One-line cheatsheet prepended to every interactive UI message.
INTERACTIVE_INSTRUCTION_LINE = (
    "↑↓ select · Enter confirm · Esc cancel · type to enter text"
)

# Hard ceiling per Telegram message; leave headroom for entities.
_TELEGRAM_MAX_TEXT = 4096
_MAX_DIRECT_CHOICES = 8
_MIN_NUMBERED_DIRECT_CHOICES = 2
_YES_NO_CHOICE_COUNT = 2
_CURSOR_MARKER_RE = re.compile(r"^\s*[←→❯>]")
_DIRECT_CHOICE_ACTION_RE = re.compile(
    r"(?i)^\s*(?:Esc to|Enter to|Press enter to|ctrl-g to edit)"
)
_NUMBERED_OPTION_RE = re.compile(r"^\s*(?:[←→❯>]\s*)?(\d+)\.\s+(.+?)\s*$")
_MULTI_SELECT_RE = re.compile(
    r"(?i)\b(?:select|choose|pick)\b[^\n]*(?:all|multiple|more than one)\b"
)


def format_interactive_message(
    text: str,
    pane_id: str | None = None,
    pane_name: str | None = None,
) -> str:
    """Build the body of an interactive UI message.

    Prepends the navigation instruction line so users see the keyboard
    shortcuts without trial and error, and adds a pane prefix for
    non-active pane alerts. When ``pane_name`` is set, the prefix uses
    it instead of the generic word "Pane" so multi-pane teams surface
    a recognizable label (e.g. ``api-gateway (%5)`` instead of
    ``Pane (%5)``). Truncates the captured terminal text from the top
    (most recent lines win) when the combined message would exceed
    Telegram's 4096-char per-message limit.
    """
    header = t(INTERACTIVE_INSTRUCTION_LINE)
    if pane_id:
        label = pane_name.strip() if pane_name and pane_name.strip() else t("Pane")
        header = f"{header}\n\U0001f500 {label} ({pane_id}):"

    body = text
    overhead = len(header) + 1  # +1 for the newline between header and body
    if overhead + len(body) > _TELEGRAM_MAX_TEXT:
        # Drop oldest lines first; tail of the buffer is what the user needs.
        budget = _TELEGRAM_MAX_TEXT - overhead
        body = body[-budget:] if budget > 0 else ""
    return f"{header}\n{body}"


@topic_state.register("topic")
def clear_send_cooldowns(user_id: int, thread_id: int) -> None:
    """Clear send cooldown for this topic (called on topic cleanup)."""
    _send_cooldowns.pop((user_id, thread_id or 0), None)


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    ikey = (user_id, thread_id or 0)
    _interactive_mode.pop(ikey, None)
    _interactive_contexts.pop(ikey, None)
    _interactive_sequences.pop(ikey, None)
    _interactive_contents.pop(ikey, None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def _numbered_menu_blocks(
    lines: list[str],
) -> list[tuple[int, int, tuple[tuple[str, str], ...]]]:
    """Find bounded, sequential numbered menus in terminal text."""
    blocks: list[tuple[int, int, tuple[tuple[str, str], ...]]] = []
    index = 0
    while index < len(lines):
        first = _NUMBERED_OPTION_RE.fullmatch(lines[index])
        if first is None or first.group(1) != "1":
            index += 1
            continue
        start = index
        expected = 1
        choices: list[tuple[str, str]] = []
        while index < len(lines):
            option = _NUMBERED_OPTION_RE.fullmatch(lines[index])
            if option is None:
                break
            number, label = option.groups()
            if int(number) != expected:
                break
            choices.append((number, f"{number}. {label}"))
            expected += 1
            index += 1
        if len(choices) >= _MIN_NUMBERED_DIRECT_CHOICES:
            blocks.append((start, index - 1, tuple(choices)))
        if index == start:
            index += 1
    return blocks


def parse_direct_choices(content: str) -> tuple[tuple[str, str], ...]:
    """Extract safe one-tap choices from an active single-select prompt.

    Ambiguous prose, multi-select prompts, non-sequential menus and menus with
    more than eight entries deliberately keep the navigation-only keyboard.
    """
    if "☐" in content or "☑" in content or _MULTI_SELECT_RE.search(content):
        return ()

    lines = content.splitlines()
    blocks = _numbered_menu_blocks(lines)
    anchors = [
        index
        for index, line in enumerate(lines)
        if _CURSOR_MARKER_RE.search(line) or _DIRECT_CHOICE_ACTION_RE.search(line)
    ]
    if anchors:
        ranked = sorted(
            (
                min(min(abs(anchor - start), abs(anchor - end)) for anchor in anchors),
                choices,
            )
            for start, end, choices in blocks
        )
        if ranked:
            nearest_distance, nearest_choices = ranked[0]
            if (
                len(nearest_choices) <= _MAX_DIRECT_CHOICES
                and sum(distance == nearest_distance for distance, _ in ranked) == 1
            ):
                return nearest_choices

    words = re.findall(r"(?m)^\s*(?:[←→❯>]\s*)?(Yes|No)\s*$", content, re.I)
    if {word.lower() for word in words} == {"yes", "no"} and len(
        words
    ) == _YES_NO_CHOICE_COUNT:
        return (("y", t("Yes")), ("n", t("No")))
    inline_yes_no = re.search(
        r"(?mi)^\s*(?:[←→❯>]\s*)?Yes\s+(?:[←→❯>]\s*)?No\s*$", content
    )
    return (("y", t("Yes")), ("n", t("No"))) if inline_yes_no else ()


def _next_interactive_sequence(ikey: tuple[int, int], content: str) -> int:
    """Advance the prompt generation only when rendered content changes."""
    if _interactive_contents.get(ikey) != content:
        _interactive_contents[ikey] = content
        _interactive_sequences[ikey] = _interactive_sequences.get(ikey, 0) + 1
    return _interactive_sequences.setdefault(ikey, 1)


def is_current_interactive_prompt(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    chat_id: int | None,
    message_id: int | None,
    sequence: int,
) -> bool:
    """Return whether a direct-choice callback is current and unconsumed."""
    if chat_id is None or message_id is None:
        return False
    ikey = (user_id, thread_id or 0)
    return (
        _interactive_mode.get(ikey) == window_id
        and _interactive_msgs.get(ikey) == message_id
        and _interactive_contexts.get(ikey) == (chat_id, message_id)
        and _interactive_sequences.get(ikey) == sequence
    )


def advance_interactive_sequence(user_id: int, thread_id: int | None = None) -> None:
    """Invalidate direct choices after one successful delivery."""
    ikey = (user_id, thread_id or 0)
    if ikey in _interactive_sequences:
        _interactive_sequences[ikey] += 1


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
    pane_id: str | None = None,
    direct_choices: tuple[tuple[str, str], ...] = (),
    sequence: int = 0,
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    ``ui_name`` controls the layout: ``RestoreCheckpoint`` omits ←/→ keys
    since only vertical selection is needed.

    When ``pane_id`` is set, it is appended to each callback data so
    responses route to a specific pane instead of the window's active pane.
    """
    # Lazy: pane delimiter constant
    from ..callback_data import CB_PANE_DELIMITER

    vertical_only = ui_name == "RestoreCheckpoint"
    # Target suffix: "@12" or "@12|%5" (tmux) / "w2:t1|w2:p1" (herdr) when pane-targeted
    target = f"{window_id}{CB_PANE_DELIMITER}{pane_id}" if pane_id else window_id

    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, len(direct_choices), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_ASK_CHOICE}{key}:{sequence}:{target}"[:64],
                )
                for key, label in direct_choices[start : start + 2]
            ]
        )
    # Row 1: directional keys
    rows.append(
        [
            InlineKeyboardButton(
                t("␣ Space"), callback_data=f"{CB_ASK_SPACE}{target}"[:64]
            ),
            InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{target}"[:64]),
            InlineKeyboardButton(
                t("⇥ Tab"), callback_data=f"{CB_ASK_TAB}{target}"[:64]
            ),
        ]
    )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton("↓", callback_data=f"{CB_ASK_DOWN}{target}"[:64]),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton("←", callback_data=f"{CB_ASK_LEFT}{target}"[:64]),
                InlineKeyboardButton("↓", callback_data=f"{CB_ASK_DOWN}{target}"[:64]),
                InlineKeyboardButton("→", callback_data=f"{CB_ASK_RIGHT}{target}"[:64]),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                t("⎋ Esc"), callback_data=f"{CB_ASK_ESC}{target}"[:64]
            ),
            InlineKeyboardButton("🔄", callback_data=f"{CB_ASK_REFRESH}{target}"[:64]),
            InlineKeyboardButton(
                t("⏎ Enter"), callback_data=f"{CB_ASK_ENTER}{target}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _edit_interactive_msg(
    client: TelegramClient,
    chat_id: int,
    msg_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup,
    ikey: tuple[int, int],
    window_id: str,
) -> bool | None:
    """Try to edit an existing interactive message.

    Returns True/False on success/failure, or None if no edit was attempted.
    """
    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
        )
        _interactive_mode[ikey] = window_id
        return True
    except BadRequest as e:
        if "Message is not modified" in e.message:
            return True  # Content identical, no-op
        logger.warning("BadRequest editing interactive msg: %s", e.message)
        return False
    except RetryAfter:
        raise
    except TelegramError:
        logger.warning("Failed to edit interactive message", exc_info=True)
        return False


async def _capture_interactive_content(
    window_id: str,
    pane_id: str | None = None,
) -> tuple[str, str] | None:
    """Capture pane and extract interactive UI content.

    When *pane_id* is given, captures that specific pane (by stable ``%N`` ID)
    instead of the window's active pane.

    Returns (ui_name, text) if an interactive UI is detected, None otherwise.
    """
    if pane_id:
        pane_text = await tmux_manager.capture_pane_by_id(pane_id, window_id=window_id)
    else:
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            return None
        pane_text = await tmux_manager.capture_pane(w.window_id)

    if not pane_text:
        logger.debug(
            "No pane text captured for window_id %s pane_id %s", window_id, pane_id
        )
        return None

    provider = get_provider_for_window(
        window_id, provider_name=get_window_provider(window_id)
    )
    pane_title = ""
    if provider.capabilities.uses_pane_title and not pane_id:
        pane_title = await tmux_manager.get_pane_title(window_id)
    status = provider.parse_terminal_status(pane_text, pane_title=pane_title)
    if status is None or not status.is_interactive:
        return None

    if not status.ui_type:
        logger.warning(
            "Interactive status with no ui_type in window_id %s pane %s",
            window_id,
            pane_id,
        )
        return None

    return status.ui_type, status.raw_text


def _lookup_pane_name(window_id: str, pane_id: str) -> str | None:
    """Return the user-supplied pane name if recorded, else None."""
    # Lazy: window_state_ports wiring is bootstrapped after this module
    # is registered as a callback target; keep at call site.
    from ...window_state_ports.pane_state import get_pane_projection

    pane = get_pane_projection(window_id, pane_id)
    return pane.name if pane else None


async def _send_interactive_with_retry(
    client: TelegramClient,
    *,
    chat_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup,
    thread_kwargs: dict[str, int],
    ikey: tuple[int, int],
    thread_id: int | None,
    window_id: str,
    now: float,
) -> Message | None:
    """Send interactive UI with one retry on transient transport errors."""
    for attempt in range(_INTERACTIVE_SEND_RETRIES + 1):
        try:
            return await client.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                **thread_kwargs,  # type: ignore[arg-type]
            )
        except BadRequest as e:
            if is_thread_gone(e):
                logger.warning(
                    "Topic gone for interactive UI (chat=%s thread=%s window=%s), "
                    "backing off %ss — use /sync to recreate",
                    chat_id,
                    thread_id,
                    window_id,
                    int(_DEAD_TOPIC_RETRY_INTERVAL),
                )
                _send_cooldowns[ikey] = (
                    now + _DEAD_TOPIC_RETRY_INTERVAL - _SEND_RETRY_INTERVAL
                )
            else:
                logger.error("Failed to send interactive UI to %s: %s", chat_id, e)
            return None
        except (TimedOut, NetworkError) as e:
            if attempt < _INTERACTIVE_SEND_RETRIES:
                logger.debug("Interactive UI send transient error, retrying: %s", e)
                await asyncio.sleep(_INTERACTIVE_SEND_RETRY_BACKOFF_S)
                continue
            logger.error("Failed to send interactive UI to %s: %s", chat_id, e)
            return None
        except TelegramError as e:
            logger.error("Failed to send interactive UI to %s: %s", chat_id, e)
            return None
    return None


async def handle_interactive_ui(
    client: TelegramClient,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    pane_id: str | None = None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.

    When *pane_id* is given, captures and targets a specific pane (for
    multi-pane windows such as agent teams).  The pane context is shown
    in the message and the keyboard routes responses to that pane.
    """
    captured = await _capture_interactive_content(window_id, pane_id=pane_id)
    if not captured:
        return False

    ui_name, content = captured
    pane_name = _lookup_pane_name(window_id, pane_id) if pane_id else None
    text = format_interactive_message(content, pane_id=pane_id, pane_name=pane_name)
    ikey = (user_id, thread_id or 0)
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    sequence = _next_interactive_sequence(ikey, text)
    keyboard = _build_interactive_keyboard(
        window_id,
        ui_name=ui_name,
        pane_id=pane_id,
        direct_choices=parse_direct_choices(content),
        sequence=sequence,
    )

    # Try editing existing interactive message first
    existing_msg_id = _interactive_msgs.get(ikey)
    if existing_msg_id:
        edited = await _edit_interactive_msg(
            client, chat_id, existing_msg_id, text, keyboard, ikey, window_id
        )
        if edited:
            _interactive_contexts[ikey] = (chat_id, existing_msg_id)
        return edited or False

    # Cooldown: prevent rapid retries when sends fail
    now = time.monotonic()
    last_attempt = _send_cooldowns.get(ikey, 0.0)
    if now - last_attempt < _SEND_RETRY_INTERVAL:
        return False

    # Send new message
    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    _send_cooldowns[ikey] = now
    # Send as plain text — terminal content should not be formatted.
    await rate_limit_send(chat_id)
    sent = await _send_interactive_with_retry(
        client,
        chat_id=chat_id,
        text=text,
        keyboard=keyboard,
        thread_kwargs=thread_kwargs,
        ikey=ikey,
        thread_id=thread_id,
        window_id=window_id,
        now=now,
    )
    if sent:
        _interactive_msgs[ikey] = sent.message_id
        _interactive_contexts[ikey] = (chat_id, sent.message_id)
        _interactive_mode[ikey] = window_id
        _send_cooldowns.pop(ikey, None)
    return sent is not None


async def clear_interactive_msg(
    user_id: int,
    client: TelegramClient | None = None,
    thread_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    _interactive_contexts.pop(ikey, None)
    _interactive_sequences.pop(ikey, None)
    _interactive_contents.pop(ikey, None)
    _send_cooldowns.pop(ikey, None)
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if client and msg_id:
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        with contextlib.suppress(TelegramError):
            await client.delete_message(chat_id=chat_id, message_id=msg_id)
