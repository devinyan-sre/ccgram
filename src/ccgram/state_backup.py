"""Rotating snapshots and corruption recovery for JSON state files.

``state.json`` holds every topic↔window binding. It was previously a single
copy with no backup, and a parse failure degraded to an empty dict — after
which the next debounced save overwrote the damaged file with ``{}``, turning
recoverable corruption into permanent loss of every binding.

This module gives state files a rotating snapshot history plus a recovery
path:

- :func:`snapshot` keeps the last :data:`KEEP_SNAPSHOTS` known-good copies in
  a ``backups/`` directory beside the state file.
- :func:`preserve_corrupt` moves a damaged file aside (never deletes it) so a
  human can still inspect it.
- :func:`newest_snapshot` / :func:`restore_from` power both automatic recovery
  at load time and the manual ``ccgram doctor --restore`` flow.

Snapshot filenames embed a counter, not a wall-clock timestamp, so rotation
stays deterministic and testable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import structlog

logger = structlog.get_logger()

# How many known-good copies to retain per state file.
KEEP_SNAPSHOTS = 5

_BACKUP_DIRNAME = "backups"


def backup_dir(path: Path) -> Path:
    """Directory holding snapshots for ``path``."""
    return path.parent / _BACKUP_DIRNAME


def _rotation_index(path: Path, candidate: Path) -> int | None:
    """Rotation counter of a snapshot filename, or None if not a snapshot.

    Only ``<name>.<digits>`` counts. Preserved corrupt files are named
    ``<name>.corrupt.<digits>`` and must never be treated as restorable —
    restoring one would write the damaged content straight back.
    """
    prefix = f"{path.name}."
    if not candidate.name.startswith(prefix):
        return None
    suffix = candidate.name[len(prefix) :]
    return int(suffix) if suffix.isdigit() else None


def list_snapshots(path: Path) -> list[Path]:
    """Known-good snapshots for ``path``, newest first (corrupt files excluded)."""
    directory = backup_dir(path)
    if not directory.is_dir():
        return []
    snaps = [
        p
        for p in directory.glob(f"{path.name}.*")
        if p.is_file() and _rotation_index(path, p) is not None
    ]
    # Sort by mtime rather than name so a restored/copied file still orders
    # correctly, with the name as a stable tiebreaker.
    return sorted(snaps, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def list_corrupt(path: Path) -> list[Path]:
    """Preserved corrupt copies of ``path``, newest first."""
    directory = backup_dir(path)
    if not directory.is_dir():
        return []
    corrupt = [p for p in directory.glob(f"{path.name}.corrupt.*") if p.is_file()]
    return sorted(corrupt, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def newest_snapshot(path: Path) -> Path | None:
    """Most recent snapshot for ``path``, or None when there is none."""
    snaps = list_snapshots(path)
    return snaps[0] if snaps else None


def snapshot(path: Path) -> Path | None:
    """Copy ``path`` into the backup directory, rotating old snapshots.

    Best-effort: a failure to snapshot must never block the caller, so errors
    are logged and swallowed. Returns the snapshot path, or None if nothing
    was written.
    """
    if not path.is_file():
        return None
    directory = backup_dir(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        existing = list_snapshots(path)
        # Name by rotation counter; collisions are avoided by removing the
        # oldest entries below.
        target = directory / f"{path.name}.{_next_index(existing, path)}"
        shutil.copy2(path, target)
        for stale in list_snapshots(path)[KEEP_SNAPSHOTS:]:
            stale.unlink(missing_ok=True)
        return target
    except OSError as exc:
        logger.warning("State snapshot failed", path=str(path), error=str(exc))
        return None


def _next_index(existing: list[Path], path: Path) -> int:
    """Next rotation counter, one past the highest existing suffix."""
    indices = [i for p in existing if (i := _rotation_index(path, p)) is not None]
    return max(indices, default=0) + 1


def preserve_corrupt(path: Path) -> Path | None:
    """Move a damaged state file aside so it is never silently overwritten.

    Returns the preserved path, or None when nothing could be preserved.
    """
    if not path.is_file():
        return None
    directory = backup_dir(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        index = len(list_corrupt(path)) + 1
        target = directory / f"{path.name}.corrupt.{index}"
        shutil.copy2(path, target)
        return target
    except OSError as exc:
        logger.warning(
            "Could not preserve corrupt state file", path=str(path), error=str(exc)
        )
        return None


def restore_from(snapshot_path: Path, path: Path) -> bool:
    """Copy ``snapshot_path`` over ``path``. Returns True on success."""
    try:
        shutil.copy2(snapshot_path, path)
    except OSError as exc:
        logger.error(
            "State restore failed",
            snapshot=str(snapshot_path),
            path=str(path),
            error=str(exc),
        )
        return False
    return True
