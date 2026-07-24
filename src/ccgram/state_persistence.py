"""Debounced, atomic JSON state persistence.

Extracted from SessionManager to provide reusable state saving with:
  - schedule_save(): debounced 0.5s save (resets on each call).
  - do_save(serialize_fn): atomic write via temp+rename.
  - flush(): immediate save if dirty.
  - load(): read JSON and return raw dict.
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from . import state_backup
from .utils import atomic_write_json

logger = structlog.get_logger()

_SaveError = (OSError, TypeError, ValueError)


class StatePersistence:
    """Debounced, atomic JSON file persistence."""

    def __init__(self, path: Path, serialize_fn: Callable[[], dict[str, Any]]) -> None:
        self._path = path
        self._serialize_fn = serialize_fn
        self._save_timer: asyncio.TimerHandle | None = None
        self._dirty = False

    def schedule_save(self) -> None:
        """Schedule debounced save (0.5s delay, resets on each call)."""
        self._dirty = True
        if self._save_timer is not None:
            self._save_timer.cancel()
        try:
            loop = asyncio.get_running_loop()
            self._save_timer = loop.call_later(0.5, self._do_save)
        except RuntimeError:
            self._do_save()  # No event loop (tests) -> immediate

    def _do_save(self) -> None:
        """Actual write via atomic_write_json."""
        self._save_timer = None
        try:
            state = self._serialize_fn()
            atomic_write_json(self._path, state)
            self._dirty = False
        except _SaveError:
            logger.exception("Failed to save state")

    def flush(self) -> None:
        """Force immediate save. Call on shutdown."""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        if self._dirty:
            self._do_save()

    def load(self) -> dict[str, Any]:
        """Read the state file, recovering from corruption where possible.

        A damaged file used to degrade to ``{}``, after which the next
        debounced save overwrote it — turning recoverable corruption into
        permanent loss of every topic binding. Now the damaged file is
        preserved and the newest known-good snapshot is restored in its place;
        only if there is no snapshot do we fall back to empty state.

        A successful load takes a snapshot, so the next run always has a
        known-good copy to fall back to.
        """
        if not self._path.exists():
            return {}
        try:
            state = json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError, OSError) as e:
            return self._recover(e)
        state_backup.snapshot(self._path)
        return state

    def _recover(self, error: Exception) -> dict[str, Any]:
        """Preserve the damaged file and restore the newest snapshot."""
        preserved = state_backup.preserve_corrupt(self._path)
        snapshot = state_backup.newest_snapshot(self._path)
        if snapshot is not None and state_backup.restore_from(snapshot, self._path):
            try:
                state = json.loads(self._path.read_text())
            except json.JSONDecodeError, ValueError, OSError:
                logger.error(
                    "State snapshot is also unreadable; starting empty",
                    path=str(self._path),
                    snapshot=str(snapshot),
                    preserved=str(preserved),
                )
                return {}
            logger.error(
                "State file was corrupt — restored from snapshot",
                path=str(self._path),
                error=str(error),
                snapshot=str(snapshot),
                preserved=str(preserved),
            )
            return state
        logger.error(
            "State file was corrupt and no snapshot was available; "
            "starting with empty state",
            path=str(self._path),
            error=str(error),
            preserved=str(preserved),
        )
        return {}
