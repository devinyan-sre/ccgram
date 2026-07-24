"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCGRAM_DIR/.env (default ~/.ccgram).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import structlog
import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import ccgram_dir

logger = structlog.get_logger()


def _parse_int_env(name: str, default: int) -> int:
    """Parse an integer from an env var with a clear error on bad values."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid integer: {exc}") from exc


def _resolve_toolbar_path() -> str:
    """Resolve the toolbar TOML config path: env var → ~/.ccgram → empty.

    Order:
      1. ``$CCGRAM_TOOLBAR_CONFIG`` if set (used as-is, even if missing)
      2. ``~/.ccgram/toolbar.toml`` if it exists
      3. ``""`` (use built-in defaults)
    """
    env = os.getenv("CCGRAM_TOOLBAR_CONFIG", "").strip()
    if env:
        return env
    fallback = ccgram_dir() / "toolbar.toml"
    return str(fallback) if fallback.exists() else ""


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccgram_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        for env_path in (Path(".env"), self.config_dir / ".env"):
            if env_path.is_file():
                load_dotenv(env_path)
                logger.debug("Loaded env from %s", env_path.resolve())

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccgram")
        self.tmux_main_window_name = "__main__"
        # Own tmux window ID (set by run_bot() after auto-detect, used to skip self in list_windows)
        self.own_window_id: str | None = None

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"
        self.events_file = self.config_dir / "events.jsonl"

        # Claude Code session monitoring configuration
        _claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        self.claude_config_dir: Path = (
            Path(_claude_config_dir).expanduser()
            if _claude_config_dir
            else Path.home() / ".claude"
        )
        self.claude_projects_path = self.claude_config_dir / "projects"
        self.monitor_poll_interval = max(
            0.5, float(os.getenv("MONITOR_POLL_INTERVAL", "1.0"))
        )
        self.status_poll_interval = max(
            0.5, float(os.getenv("CCGRAM_STATUS_POLL_INTERVAL", "1.0"))
        )

        self._load_monitoring_env()

        # Quiet hours: "HH:MM-HH:MM" local time; automated notifications are
        # delivered silently inside the window. Empty disables.
        self.quiet_hours = os.getenv("CCGRAM_QUIET_HOURS", "").strip()

        # Daily digest: "HH:MM" local time to post a per-topic activity
        # summary to the group's General topic. Empty disables.
        self.daily_digest_time = os.getenv("CCGRAM_DAILY_DIGEST", "").strip()

        # Multi-instance support
        group_id_str = os.getenv("CCGRAM_GROUP_ID")
        if group_id_str:
            try:
                self.group_id: int | None = int(group_id_str)
            except ValueError as e:
                raise ValueError(f"CCGRAM_GROUP_ID must be a valid integer: {e}") from e
        else:
            self.group_id = None

        # Provider selection
        self.provider_name: str = os.getenv("CCGRAM_PROVIDER", "claude")

        # Terminal-multiplexer backend selection (tmux default; herdr opt-in)
        self.multiplexer_name: str = os.getenv("CCGRAM_MULTIPLEXER", "tmux")

        # Directory browser: show hidden (dot) directories
        self.show_hidden_dirs: bool = os.getenv(
            "CCGRAM_SHOW_HIDDEN_DIRS", ""
        ).lower() in ("1", "true", "yes")

        # Ack reaction: react to forwarded messages with an emoji (empty = disabled)
        self.ack_reaction: str = os.getenv("CCGRAM_ACK_REACTION", "")

        # Whisper transcription
        self.whisper_provider: str = os.getenv("CCGRAM_WHISPER_PROVIDER", "")
        self.whisper_api_key: str = os.getenv("CCGRAM_WHISPER_API_KEY", "")
        self.whisper_base_url: str = os.getenv("CCGRAM_WHISPER_BASE_URL", "")
        self.whisper_model: str = os.getenv("CCGRAM_WHISPER_MODEL", "")
        self.whisper_language: str = os.getenv("CCGRAM_WHISPER_LANGUAGE", "")

        # Voice replies (text-to-speech)
        # CCGRAM_TTS_PROVIDER: empty = disabled; "edge" = edge-tts; "openai" = OpenAI TTS
        self.tts_provider: str = os.getenv("CCGRAM_TTS_PROVIDER", "")
        self.tts_voice: str = os.getenv(
            "CCGRAM_TTS_VOICE", "en-US-EmmaMultilingualNeural"
        )
        self.tts_model: str = os.getenv("CCGRAM_TTS_MODEL", "gpt-4o-mini-tts")
        self.tts_api_key: str = os.getenv("CCGRAM_TTS_API_KEY", "")

        # LLM command generation (shell provider) and toolbar config path.
        # toolbar_config_path resolution: env var → ~/.ccgram/toolbar.toml → "".
        # Empty string means "use built-in defaults". The handler layer passes
        # this path to ``toolbar_config.load_toolbar_config()`` once at startup.
        self._init_shell_and_llm()
        self._init_live_view()
        self._init_send()
        self._init_lifecycle()

        # Global default for hiding tool_use/tool_result content in Telegram.
        # Shown by default; set CCGRAM_HIDE_TOOL_CALLS=true to suppress globally.
        # Per-window override via WindowState.tool_call_visibility takes precedence.
        self.hide_tool_calls: bool = os.getenv(
            "CCGRAM_HIDE_TOOL_CALLS", "false"
        ).lower() in ("1", "true", "yes")

        # Global default batch mode: ephemeral tools (single rolling message deleted
        # on completion). Off by default. Per-window batch_mode takes precedence when
        # explicitly set to any value other than DEFAULT_BATCH_MODE via /verbose.
        self.ephemeral_tools: bool = os.getenv(
            "CCGRAM_EPHEMERAL_TOOLS", ""
        ).lower() in ("1", "true", "yes")

        # Color mapping for the topic state emoji prefix.
        # "system" (default): green=active, yellow=idle (system POV: green=working).
        # "user": green=idle, yellow=active (user POV: green=ready for me).
        # Invalid values fall back to "system".
        raw_status_mode = os.getenv("CCGRAM_STATUS_MODE", "").strip().lower()
        self.status_mode: str = (
            raw_status_mode if raw_status_mode in ("system", "user") else "system"
        )

        logger.debug(
            "Config initialized: dir=%s, allowed_users=%d, tmux_session=%s",
            self.config_dir,
            len(self.allowed_users),
            self.tmux_session_name,
        )

    def _load_monitoring_env(self) -> None:
        # Token/context watch. Context warning fires when the current context
        # reaches CCGRAM_CONTEXT_WARN percent of CCGRAM_CONTEXT_LIMIT tokens
        # (0 disables); the cumulative warning fires once per session past
        # CCGRAM_TOKEN_WARN total tokens (0 = disabled, the default).
        self.context_warn_pct = max(0, _parse_int_env("CCGRAM_CONTEXT_WARN", 80))
        self.context_limit_tokens = max(
            1, _parse_int_env("CCGRAM_CONTEXT_LIMIT", 200000)
        )
        self.token_warn_total = max(0, _parse_int_env("CCGRAM_TOKEN_WARN", 0))

        # Filesystem-event wakeups: watch transcript/event files and wake the
        # monitor loop immediately on writes (poll interval stays the fallback
        # cadence). Set CCGRAM_FS_EVENTS=0 to disable.
        self.fs_events_enabled: bool = os.getenv("CCGRAM_FS_EVENTS", "1").lower() in (
            "1",
            "true",
            "yes",
        )

        # Adaptive status polling: idle windows (no pane change, no transcript
        # activity for 30s) are ticked every 5th cycle instead of every cycle,
        # skipping their pane-capture subprocess. Any activity restores the
        # per-cycle cadence immediately. Set CCGRAM_ADAPTIVE_POLL=0 to disable.
        self.adaptive_poll: bool = os.getenv("CCGRAM_ADAPTIVE_POLL", "1").lower() in (
            "1",
            "true",
            "yes",
        )

        # Operator DM target for startup self-checks and error alerts. Empty
        # falls back to the lowest allowed-user id (the primary operator).
        operator_chat_str = os.getenv("CCGRAM_OPERATOR_CHAT_ID", "").strip()
        if operator_chat_str:
            try:
                self.operator_chat_id: int | None = int(operator_chat_str)
            except ValueError as e:
                raise ValueError(
                    f"CCGRAM_OPERATOR_CHAT_ID must be a valid integer: {e}"
                ) from e
        else:
            self.operator_chat_id = None

        # Fallback sink when the operator DM can't be delivered (e.g. the
        # operator never opened a private chat, so the bot "can't initiate
        # conversation"). A group/topic chat the bot can already post to. Empty
        # falls back to CCGRAM_GROUP_ID.
        fallback_chat_str = os.getenv("CCGRAM_OPERATOR_FALLBACK_CHAT_ID", "").strip()
        if fallback_chat_str:
            try:
                self.operator_fallback_chat_id: int | None = int(fallback_chat_str)
            except ValueError as e:
                raise ValueError(
                    f"CCGRAM_OPERATOR_FALLBACK_CHAT_ID must be a valid integer: {e}"
                ) from e
        else:
            self.operator_fallback_chat_id = None

        # Error-rate alerting: DM the operator when the same error signature
        # fires repeatedly in a short window. Set CCGRAM_ERROR_ALERTS=0 to
        # disable.
        self.error_alerts_enabled: bool = os.getenv(
            "CCGRAM_ERROR_ALERTS", "1"
        ).lower() in ("1", "true", "yes")

    def _init_live_view(self) -> None:
        self.live_view_interval: int = max(
            1, _parse_int_env("CCGRAM_LIVE_VIEW_INTERVAL", 5)
        )
        self.live_view_timeout: int = max(
            1, _parse_int_env("CCGRAM_LIVE_VIEW_TIMEOUT", 300)
        )

    def _init_shell_and_llm(self) -> None:
        self.prompt_mode = os.getenv("CCGRAM_PROMPT_MODE", "wrap")
        self.prompt_marker = os.getenv("CCGRAM_PROMPT_MARKER", "ccgram")
        self.toolbar_config_path: str = _resolve_toolbar_path()
        self.llm_provider: str = os.getenv("CCGRAM_LLM_PROVIDER", "")
        self.llm_api_key: str = os.getenv("CCGRAM_LLM_API_KEY", "")
        self.llm_base_url: str = os.getenv("CCGRAM_LLM_BASE_URL", "")
        self.llm_model: str = os.getenv("CCGRAM_LLM_MODEL", "")
        try:
            self.llm_temperature: float = float(
                os.getenv("CCGRAM_LLM_TEMPERATURE", "0.1")
            )
        except ValueError as e:
            raise ValueError(
                f"CCGRAM_LLM_TEMPERATURE must be a valid number: {e}"
            ) from e

    def _init_send(self) -> None:
        self.send_search_depth: int = _parse_int_env("CCGRAM_SEND_SEARCH_DEPTH", 5)
        self.send_max_results: int = _parse_int_env("CCGRAM_SEND_MAX_RESULTS", 50)

    def _init_lifecycle(self) -> None:
        self.autoclose_done_minutes: int = int(
            os.getenv("AUTOCLOSE_DONE_MINUTES", "30")
        )
        self.autoclose_dead_minutes: int = int(
            os.getenv("AUTOCLOSE_DEAD_MINUTES", "10")
        )
        self.pane_lifecycle_notify: bool = os.getenv(
            "CCGRAM_PANE_LIFECYCLE_NOTIFY", ""
        ).lower() in ("1", "true", "yes")
        self._init_miniapp()
        self._init_metrics()

    def _init_metrics(self) -> None:
        # Metrics/health listener — off by default (port 0). Independent of the
        # Mini App: operators need /metrics and /healthz whenever the bot runs,
        # not only when the optional dashboard is enabled. Binds to loopback by
        # default so nothing is public without an explicit reverse proxy.
        self.metrics_host: str = os.getenv("CCGRAM_METRICS_HOST", "127.0.0.1")
        self.metrics_port: int = max(0, _parse_int_env("CCGRAM_METRICS_PORT", 0))

    def _init_miniapp(self) -> None:
        # Mini App backend (Phase 3 / Theme 6) — disabled when base URL is empty.
        # base_url is the externally reachable URL Telegram uses to open the
        # WebApp; host/port control the local aiohttp listener.
        self.miniapp_base_url: str = os.getenv("CCGRAM_MINIAPP_BASE_URL", "").strip()
        self.miniapp_host: str = os.getenv("CCGRAM_MINIAPP_HOST", "127.0.0.1")
        self.miniapp_port: int = _parse_int_env("CCGRAM_MINIAPP_PORT", 8765)

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users


config = Config()
