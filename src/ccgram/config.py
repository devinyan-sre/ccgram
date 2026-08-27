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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _parse_user_ids(name: str) -> set[int]:
    """Parse an optional comma-separated Telegram user-ID set."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    try:
        return {int(value.strip()) for value in raw.split(",") if value.strip()}
    except ValueError as exc:
        raise ValueError(
            f"{name} contains a non-numeric value: {exc}. "
            "Expected comma-separated Telegram user IDs."
        ) from exc


_MAX_PORT = 65535
_MAX_PERCENT = 100
_VALID_MULTIPLEXERS = frozenset({"tmux", "herdr"})
_VALID_STATUS_MODES = frozenset({"system", "user"})
_VALID_LANG_PREFIXES = frozenset({"en", "zh"})


def _looks_like_time_spec(raw: str, expected: str) -> bool:
    """Shape-check an ``HH:MM`` or ``HH:MM-HH:MM`` value.

    Deliberately a shape check, not a parse: the owning feature still does its
    own strict parsing. This exists so an obviously malformed value surfaces at
    startup instead of silently disabling the feature.
    """
    parts = raw.split("-") if "-" in expected else [raw]
    if len(parts) != len(expected.split("-")):
        return False
    for part in parts:
        hhmm = part.strip().split(":")
        if len(hhmm) != 2:  # noqa: PLR2004 — HH and MM
            return False
        hours, minutes = hhmm
        if not (hours.isdigit() and minutes.isdigit()):
            return False
        if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):  # noqa: PLR2004
            return False
    return True


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

    def __init__(self) -> None:  # noqa: PLR0915
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

        # Time zone used only for human-facing timestamps and scheduled local
        # jobs. Durable state and ordering remain UTC epoch/ISO values.
        self.timezone_name: str = os.getenv("CCGRAM_TIMEZONE", "UTC").strip() or "UTC"

        # Optional RBAC. When no role variables are configured every legacy
        # allow-listed user remains an admin, preserving existing installs.
        configured_admins = _parse_user_ids("CCGRAM_ADMINS")
        configured_operators = _parse_user_ids("CCGRAM_OPERATORS")
        configured_viewers = _parse_user_ids("CCGRAM_VIEWERS")
        if configured_admins or configured_operators or configured_viewers:
            self.admin_users = configured_admins
            self.viewer_users = configured_viewers - configured_admins
            self.operator_users = (
                configured_operators | (self.allowed_users - configured_viewers)
            ) - configured_admins
            self.allowed_users |= (
                self.admin_users | self.operator_users | self.viewer_users
            )
        else:
            self.admin_users = set(self.allowed_users)
            self.operator_users: set[int] = set()
            self.viewer_users: set[int] = set()

        # Multi-operator topic lanes.  A Telegram forum topic remains the
        # workspace boundary while every allow-listed member receives an
        # independent provider process/session.  Keeping this opt-in preserves
        # the historic one-user/one-window behaviour for existing installs.
        self.member_lanes_enabled: bool = os.getenv(
            "CCGRAM_MEMBER_LANES", "false"
        ).lower() in ("1", "true", "yes")
        self.require_mention_in_groups: bool = os.getenv(
            "CCGRAM_REQUIRE_MENTION",
            "true" if self.member_lanes_enabled else "false",
        ).lower() in ("1", "true", "yes")
        self.max_concurrent_updates: int = max(
            1, _parse_int_env("CCGRAM_MAX_CONCURRENT_UPDATES", 8)
        )
        self.max_member_lanes_per_topic: int = max(
            1, _parse_int_env("CCGRAM_MAX_MEMBER_LANES_PER_TOPIC", 8)
        )
        self.max_parallel_per_topic: int = max(
            1, _parse_int_env("CCGRAM_MAX_PARALLEL_PER_TOPIC", 2)
        )
        self.max_parallel_global: int = max(
            1, _parse_int_env("CCGRAM_MAX_PARALLEL_GLOBAL", 4)
        )
        self.task_lease_seconds: int = max(
            60, _parse_int_env("CCGRAM_TASK_LEASE_SECONDS", 7200)
        )
        self.message_coalesce_ms: int = max(
            0, _parse_int_env("CCGRAM_MESSAGE_COALESCE_MS", 0)
        )
        self.max_task_supplements: int = max(
            1, _parse_int_env("CCGRAM_MAX_TASK_SUPPLEMENTS", 20)
        )
        self.task_queue_alert_seconds: int = max(
            0, _parse_int_env("CCGRAM_TASK_QUEUE_ALERT_SECONDS", 300)
        )
        self.task_cancel_confirm_seconds: int = max(
            1, _parse_int_env("CCGRAM_TASK_CANCEL_CONFIRM_SECONDS", 8)
        )
        self.task_estimate_default_seconds: int = max(
            1, _parse_int_env("CCGRAM_TASK_ESTIMATE_DEFAULT_SECONDS", 300)
        )
        # Telegram operations dashboard.  Disabled by default so existing
        # installations never receive new/pinned messages after an upgrade.
        # ``general`` renders one group-wide view, ``topic`` renders one view
        # per bound workspace topic, and ``both`` enables both scopes.
        self.dashboard_enabled: bool = os.getenv(
            "CCGRAM_DASHBOARD_ENABLED", "false"
        ).lower() in ("1", "true", "yes")
        raw_dashboard_scope = (
            os.getenv("CCGRAM_DASHBOARD_SCOPE", "general").strip().lower()
        )
        self.dashboard_scope: str = (
            raw_dashboard_scope
            if raw_dashboard_scope in ("general", "topic", "both")
            else "general"
        )
        self.dashboard_refresh_seconds: int = max(
            2, _parse_int_env("CCGRAM_DASHBOARD_REFRESH_SECONDS", 5)
        )
        self.dashboard_completed_ttl_seconds: int = max(
            0, _parse_int_env("CCGRAM_DASHBOARD_COMPLETED_TTL_SECONDS", 180)
        )
        self.dashboard_max_items: int = min(
            50, max(1, _parse_int_env("CCGRAM_DASHBOARD_MAX_ITEMS", 20))
        )
        self.dashboard_pin: bool = os.getenv(
            "CCGRAM_DASHBOARD_PIN", "true"
        ).lower() in ("1", "true", "yes")
        self.dashboard_missing_topic_failures: int = max(
            1, _parse_int_env("CCGRAM_DASHBOARD_MISSING_TOPIC_FAILURES", 2)
        )
        raw_dashboard_privacy = (
            os.getenv("CCGRAM_DASHBOARD_PRIVACY", "normal").strip().lower()
        )
        self.dashboard_privacy: str = (
            raw_dashboard_privacy
            if raw_dashboard_privacy in ("normal", "strict")
            else "normal"
        )
        self.inbound_dedupe_hours: int = max(
            1, _parse_int_env("CCGRAM_INBOUND_DEDUPE_HOURS", 72)
        )
        self.delivery_lag_warn_seconds: int = max(
            30, _parse_int_env("CCGRAM_DELIVERY_LAG_WARN_SECONDS", 120)
        )
        self.delivery_lag_min_bytes: int = max(
            1, _parse_int_env("CCGRAM_DELIVERY_LAG_MIN_BYTES", 4096)
        )
        self.media_group_coalesce_ms: int = max(
            100, _parse_int_env("CCGRAM_MEDIA_GROUP_COALESCE_MS", 750)
        )
        self.member_lane_worktrees: bool = os.getenv(
            "CCGRAM_MEMBER_LANE_WORKTREES", "true"
        ).lower() in ("1", "true", "yes")
        self.allow_shared_member_cwd: bool = os.getenv(
            "CCGRAM_ALLOW_SHARED_MEMBER_CWD", "false"
        ).lower() in ("1", "true", "yes")

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccgram")
        self.tmux_main_window_name = "__main__"
        # Own tmux window ID (set by run_bot() after auto-detect, used to skip self in list_windows)
        self.own_window_id: str | None = None

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"
        self.outbox_file = self.config_dir / "outbox.json"
        self.inbound_file = self.config_dir / "inbound.json"
        self.task_state_file = self.config_dir / "tasks.json"
        self.dashboard_state_file = self.config_dir / "dashboard.json"
        self.task_audit_file = self.config_dir / "task-audit.jsonl"
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

        # Quiet hours: "HH:MM-HH:MM" in CCGRAM_TIMEZONE; automated
        # notifications are delivered silently inside the window. Empty disables.
        self.quiet_hours = os.getenv("CCGRAM_QUIET_HOURS", "").strip()

        # Daily digest: "HH:MM" in CCGRAM_TIMEZONE to post a per-topic activity
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
        # Voice confirmation is safer by default; trusted dictation workflows
        # can opt into immediate delivery.
        self.voice_autosend: bool = os.getenv(
            "CCGRAM_VOICE_AUTOSEND", "false"
        ).lower() in ("1", "true", "yes")
        # Hide only transient working/idle status bubbles. Replies and command
        # controls remain available through their normal paths.
        self.hide_status: bool = os.getenv("CCGRAM_HIDE_STATUS", "false").lower() in (
            "1",
            "true",
            "yes",
        )

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

        # Telegram "typing…" chat action. When on, the action is refreshed only
        # while the agent is genuinely producing output (recent transcript
        # activity), so a long think/spinner phase no longer shows a misleading
        # perpetual "typing" with no message arriving. Set CCGRAM_TYPING=0 to
        # suppress the typing indicator entirely (the 🟢 topic emoji + status
        # bubble still convey a busy agent).
        self.typing_enabled: bool = os.getenv("CCGRAM_TYPING", "1").lower() in (
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

        # Destructive-action alerting: DM the operator every time unattended
        # cleanup retires a topic or kills a window. Not burst-gated — the
        # failure mode is one silent event, not a flood. CCGRAM_DESTRUCTIVE_ALERTS=0
        # keeps the audit log and metric but stops the DMs.
        self.destructive_alerts_enabled: bool = os.getenv(
            "CCGRAM_DESTRUCTIVE_ALERTS", "1"
        ).lower() in ("1", "true", "yes")

        # Mass-death circuit breaker. N window deaths inside WINDOW seconds is
        # an infrastructure event (tmux restart), not N user intentions, so
        # unattended cleanup stands down for SUSPEND minutes. The suspension is
        # far longer than the detection window on purpose: in the 2026-07-25
        # incident the destruction happened ~17 minutes after the deaths.
        # Threshold 0 disables the breaker.
        self.mass_death_threshold: int = max(
            0, _parse_int_env("CCGRAM_MASS_DEATH_THRESHOLD", 3)
        )
        self.mass_death_window_seconds: int = max(
            1, _parse_int_env("CCGRAM_MASS_DEATH_WINDOW", 120)
        )
        self.mass_death_suspend_minutes: int = max(
            1, _parse_int_env("CCGRAM_MASS_DEATH_SUSPEND", 30)
        )

        # Rehearsal mode: unattended destructive paths decide as usual and
        # record what they *would* have done, but never execute. Lets an
        # operator confirm the cleanup policy matches their intent before
        # trusting it with real topics and processes.
        self.destructive_dryrun: bool = os.getenv(
            "CCGRAM_DESTRUCTIVE_DRYRUN", ""
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
        # How an expired topic is retired. "close" (default) archives the topic
        # and keeps every message; "delete" removes it from the sidebar but
        # Telegram destroys the whole message history with it — opt-in only.
        action = os.getenv("CCGRAM_AUTOCLOSE_ACTION", "close").strip().lower()
        self.autoclose_action: str = (
            action if action in ("close", "delete") else "close"
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
        # Forward-progress stall threshold for the health gate. A false
        # "unhealthy" costs a production restart, so the default is far above
        # any normal cycle (poll loops run every 1-2s). 0 disables the
        # progress check and falls back to liveness-only.
        self.health_stall_seconds: int = max(
            0, _parse_int_env("CCGRAM_HEALTH_STALL_SEC", 120)
        )
        # Outbound queue backpressure. Past this depth transient status updates
        # are shed; agent output is only shed at twice this. 0 = unbounded.
        self.queue_max_depth: int = max(
            0, _parse_int_env("CCGRAM_QUEUE_MAX_DEPTH", 500)
        )
        # Long-idle session parking is opt-in. Incoming topic text wakes a
        # parked provider automatically and forwards that same message.
        self.auto_park_days: int = max(0, _parse_int_env("CCGRAM_AUTO_PARK_DAYS", 0))
        self.auto_park_notice_hours: int = max(
            0, _parse_int_env("CCGRAM_AUTO_PARK_NOTICE_HOURS", 24)
        )
        self.member_lane_cleanup_days: int = max(
            0, _parse_int_env("CCGRAM_MEMBER_LANE_CLEANUP_DAYS", 0)
        )

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

    def user_role(self, user_id: int) -> str | None:
        """Return ``admin``, ``operator`` or ``viewer`` for an allowed user."""
        if user_id in self.admin_users:
            return "admin"
        if user_id in self.viewer_users:
            return "viewer"
        if user_id in self.operator_users or user_id in self.allowed_users:
            return "operator"
        return None

    def validate(self) -> tuple[list[str], list[str]]:  # noqa: C901
        """Check env values, returning ``(fatal, warnings)`` problem descriptions.

        Split by blast radius rather than treating every bad value the same:

        - *fatal* — values that would break the service or silently mis-route
          it (an unbindable port, an unknown multiplexer backend). Refusing to
          start is better than running wrong.
        - *warnings* — values that were silently corrected to a default. These
          used to vanish without trace, so a typo in e.g. ``CCGRAM_STATUS_MODE``
          produced the wrong behaviour with no signal at all. Refusing to boot
          over a cosmetic typo would be worse than the typo.

        Pure: returns problems for the caller to act on, never exits or logs.
        """
        fatal: list[str] = []
        warnings: list[str] = []

        for name, port in (
            ("CCGRAM_METRICS_PORT", self.metrics_port),
            ("CCGRAM_MINIAPP_PORT", self.miniapp_port),
        ):
            if not 0 <= port <= _MAX_PORT:
                fatal.append(f"{name}={port} is not a valid TCP port (0-{_MAX_PORT})")

        if self.multiplexer_name not in _VALID_MULTIPLEXERS:
            fatal.append(
                f"CCGRAM_MULTIPLEXER={self.multiplexer_name!r} is unknown "
                f"(expected one of: {', '.join(sorted(_VALID_MULTIPLEXERS))})"
            )

        raw_status_mode = os.getenv("CCGRAM_STATUS_MODE", "").strip().lower()
        if raw_status_mode and raw_status_mode not in _VALID_STATUS_MODES:
            warnings.append(
                f"CCGRAM_STATUS_MODE={raw_status_mode!r} is not recognised; "
                f"using {self.status_mode!r} "
                f"(expected one of: {', '.join(sorted(_VALID_STATUS_MODES))})"
            )

        raw_lang = os.getenv("CCGRAM_LANG", "").strip().lower()
        if raw_lang and not any(raw_lang.startswith(p) for p in _VALID_LANG_PREFIXES):
            warnings.append(
                f"CCGRAM_LANG={raw_lang!r} is not recognised; falling back to English "
                f"(expected one of: {', '.join(sorted(_VALID_LANG_PREFIXES))})"
            )

        raw_dashboard_scope = os.getenv("CCGRAM_DASHBOARD_SCOPE", "").strip().lower()
        if raw_dashboard_scope and raw_dashboard_scope not in (
            "general",
            "topic",
            "both",
        ):
            warnings.append(
                f"CCGRAM_DASHBOARD_SCOPE={raw_dashboard_scope!r} is not recognised; "
                "using 'general' (expected one of: general, topic, both)"
            )

        raw_dashboard_privacy = (
            os.getenv("CCGRAM_DASHBOARD_PRIVACY", "").strip().lower()
        )
        if raw_dashboard_privacy and raw_dashboard_privacy not in (
            "normal",
            "strict",
        ):
            warnings.append(
                f"CCGRAM_DASHBOARD_PRIVACY={raw_dashboard_privacy!r} is not "
                "recognised; using 'normal' (expected one of: normal, strict)"
            )

        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            fatal.append(
                f"CCGRAM_TIMEZONE={self.timezone_name!r} is not a valid IANA "
                "time zone (for Beijing use 'Asia/Shanghai')"
            )

        roles_configured = any(
            os.getenv(name, "").strip()
            for name in ("CCGRAM_ADMINS", "CCGRAM_OPERATORS", "CCGRAM_VIEWERS")
        )
        if roles_configured and not self.admin_users:
            warnings.append(
                "Role-based access is configured but CCGRAM_ADMINS is empty; "
                "no user can run admin-only recovery or cleanup commands"
            )

        if not 0 <= self.context_warn_pct <= _MAX_PERCENT:
            warnings.append(
                f"CCGRAM_CONTEXT_WARN={self.context_warn_pct} is outside 0-100"
            )

        for name, raw, expected in (
            ("CCGRAM_QUIET_HOURS", self.quiet_hours, "HH:MM-HH:MM"),
            ("CCGRAM_DAILY_DIGEST", self.daily_digest_time, "HH:MM"),
        ):
            if raw and not _looks_like_time_spec(raw, expected):
                warnings.append(
                    f"{name}={raw!r} does not look like {expected}; feature disabled"
                )

        return fatal, warnings


config = Config()
