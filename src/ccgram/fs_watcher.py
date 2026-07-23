"""Filesystem watcher that wakes the session-monitor loop on writes.

Wraps a watchdog Observer (inotify on Linux) watching the Claude projects
tree and the ccgram state dir. On any ``*.jsonl`` change the asyncio wake
event is set from the observer thread via ``call_soon_threadsafe``, so the
monitor loop's poll sleep returns immediately instead of waiting out the
full interval.

Polling stays the source of truth — the watcher only shortens latency.
If watchdog is unavailable or no watched path exists, ``start()`` returns
False and the monitor degrades to plain interval polling.

Key class: TranscriptWatcher.
"""

import asyncio
import contextlib
import structlog
from pathlib import Path

logger = structlog.get_logger()


class TranscriptWatcher:
    """Watches directories for .jsonl writes and sets an asyncio wake event.

    Thread-safety: watchdog delivers events on its own observer thread; the
    only cross-thread call is ``loop.call_soon_threadsafe(event.set)``.
    """

    def __init__(
        self,
        paths: list[Path],
        wake_event: asyncio.Event,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._paths = paths
        self._wake_event = wake_event
        self._loop = loop
        self._observer = None

    def start(self) -> bool:
        """Start watching. Returns False when watching is unavailable."""
        try:
            # Lazy: watchdog is a soft dependency of the monitor — an import
            # failure must degrade to interval polling, never crash the loop.
            from watchdog.events import FileSystemEventHandler

            # Lazy: same soft-dependency rationale as the import above.
            from watchdog.observers import Observer
        except ImportError:
            logger.info("watchdog not available; monitor uses interval polling only")
            return False

        wake = self._wake

        class _JsonlHandler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:
                if event.is_directory:
                    return
                path = getattr(event, "dest_path", "") or event.src_path
                if str(path).endswith(".jsonl"):
                    wake()

        observer = Observer()
        handler = _JsonlHandler()
        scheduled = 0
        for path in self._paths:
            if not path.is_dir():
                continue
            try:
                observer.schedule(handler, str(path), recursive=True)
                scheduled += 1
            except OSError:
                logger.warning("Could not watch %s for filesystem events", path)
        if not scheduled:
            logger.info("No watchable paths; monitor uses interval polling only")
            return False

        observer.daemon = True
        observer.start()
        self._observer = observer
        logger.info(
            "Filesystem watcher active on %d path(s): %s",
            scheduled,
            ", ".join(str(p) for p in self._paths if p.is_dir()),
        )
        return True

    def _wake(self) -> None:
        # Suppressed RuntimeError: event loop already closed (shutdown race).
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._wake_event.set)

    def stop(self) -> None:
        """Stop the observer thread. Idempotent."""
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=1.0)
        self._observer = None
