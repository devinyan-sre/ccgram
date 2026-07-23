"""Monitor state persistence — tracks byte offsets for each session.

Persists TrackedSession records (session_id, file_path, last_byte_offset)
to ~/.ccgram/monitor_state.json so the session monitor can resume
incremental reading after restarts without re-sending old messages.

Crash recovery: each session carries two cursors. ``last_byte_offset`` is
the in-memory read cursor (drives incremental reads within a run);
``delivered_byte_offset`` trails it and is promoted only once the messages
parsed from those bytes reached a delivery terminal state (sent, dropped,
or failed-after-retry — see ``TranscriptReader.commit_delivered``). Only
the delivered cursor is persisted (under the existing ``last_byte_offset``
key, so the schema is unchanged in both directions): a crash between read
and delivery restarts the reader at the delivered cursor and replays the
lost batch (at-least-once).

Key classes: MonitorState, TrackedSession.
"""

import json
import structlog
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import atomic_write_json

logger = structlog.get_logger()


@dataclass
class TrackedSession:
    """State for a tracked Claude Code session."""

    session_id: str
    file_path: str  # Path to .jsonl file
    last_byte_offset: int = 0  # In-memory read cursor for incremental reading
    # Crash-safe cursor: bytes whose messages reached a delivery terminal
    # state. -1 (default) initializes it to last_byte_offset.
    delivered_byte_offset: int = -1

    def __post_init__(self) -> None:
        if self.delivered_byte_offset < 0:
            self.delivered_byte_offset = self.last_byte_offset

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization.

        Persists the *delivered* cursor under the ``last_byte_offset`` key —
        never a read position whose messages might still be undelivered —
        clamped to the read cursor (a truncation reset can move the read
        cursor below a stale delivered value).
        """
        return {
            "session_id": self.session_id,
            "file_path": self.file_path,
            "last_byte_offset": min(self.delivered_byte_offset, self.last_byte_offset),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackedSession":
        """Create from dict; both cursors start at the persisted value."""
        return cls(
            session_id=data.get("session_id", ""),
            file_path=data.get("file_path", ""),
            last_byte_offset=data.get("last_byte_offset", 0),
        )


@dataclass
class MonitorState:
    """Persistent state for the session monitor.

    Stores tracking information for all monitored sessions
    and the events.jsonl byte offset to prevent replaying
    historical hook events after restarts.
    """

    state_file: Path
    tracked_sessions: dict[str, TrackedSession] = field(default_factory=dict)
    events_offset: int = 0
    _dirty: bool = field(default=False, repr=False)

    def load(self) -> None:
        """Load state from file."""
        if not self.state_file.exists():
            logger.debug("State file does not exist: %s", self.state_file)
            return

        try:
            data = json.loads(self.state_file.read_text())
            sessions = data.get("tracked_sessions", {})
            self.tracked_sessions = {
                k: TrackedSession.from_dict(v) for k, v in sessions.items()
            }
            self.events_offset = data.get("events_offset", 0)
            logger.info(
                "Loaded %d tracked sessions from state", len(self.tracked_sessions)
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to load state file: %s", e)
            self.tracked_sessions = {}

    def save(self) -> None:
        """Save state to file atomically."""
        data = {
            "tracked_sessions": {
                k: v.to_dict() for k, v in self.tracked_sessions.items()
            },
            "events_offset": self.events_offset,
        }

        try:
            atomic_write_json(self.state_file, data)
            self._dirty = False
        except OSError:
            logger.exception("Failed to save state file")

    def get_session(self, session_id: str) -> TrackedSession | None:
        """Get tracked session by ID."""
        return self.tracked_sessions.get(session_id)

    def update_session(self, session: TrackedSession) -> None:
        """Update or add a tracked session."""
        self.tracked_sessions[session.session_id] = session
        self._dirty = True

    def remove_session(self, session_id: str) -> None:
        """Remove a tracked session."""
        if session_id in self.tracked_sessions:
            del self.tracked_sessions[session_id]
            self._dirty = True

    def save_if_dirty(self) -> None:
        """Save state only if it has been modified."""
        if self._dirty:
            self.save()
