"""Incremental event-reader for the Claude Code hook event log (events.jsonl).

Pure I/O: reads new lines from events.jsonl by byte offset, parses them as
HookEvent objects, and returns both the events and the new offset. The caller
is responsible for persisting the offset (e.g., in MonitorState).

Also owns compaction of the append-only log: compact_events_file() drops
already-consumed bytes in place (under the same flock hook.py takes for
appends) so events.jsonl stays bounded across long uptimes.

Key functions: read_new_events(), compact_events_file().
"""

import fcntl
import json
from pathlib import Path

import aiofiles
import structlog

from .hooks.state_files import StateFileValidationError, parse_event_record
from .providers.base import HookEvent

logger = structlog.get_logger()

# Compact once the consumed prefix reaches this size (checked every poll —
# a cheap int compare — plus once at monitor startup regardless of size).
EVENTS_COMPACT_THRESHOLD = 256 * 1024

# Consumed bytes are archived to <events.jsonl>.old for debugging; once the
# archive would exceed this cap it is overwritten instead of appended to,
# keeping total disk usage bounded.
_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024


def compact_events_file(path: Path, consumed_offset: int) -> int:
    """Drop the consumed prefix of events.jsonl in place; return the new offset.

    Rewrites the file under the same ``fcntl.flock`` that hook.py takes for
    appends, and never replaces the inode — so a hook process blocked on the
    lock appends to the compacted file, not an orphan. Consumed bytes are
    appended to ``<name>.old`` (overwritten once it grows past the cap).

    Synchronous (fcntl + blocking I/O) — call via ``asyncio.to_thread``.
    Returns ``consumed_offset`` unchanged on any failure so the caller's
    offset stays consistent with the untouched file.
    """
    if consumed_offset <= 0 or not path.exists():
        return consumed_offset

    try:
        with open(path, "r+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = f.read()
                if consumed_offset > len(data):
                    # External truncation/recreation: the offset no longer
                    # describes a consumed prefix of THIS file. Leave the file
                    # alone — read_new_events resets the offset to 0 and
                    # replays the (unconsumed) content.
                    return consumed_offset
                consumed = consumed_offset
                prefix, tail = data[:consumed], data[consumed:]

                if prefix:
                    _archive_consumed(path, prefix)

                f.seek(0)
                f.write(tail)
                f.truncate(len(tail))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        logger.exception("Failed to compact events file %s", path)
        return consumed_offset

    logger.info(
        "Compacted events file %s: dropped %d consumed bytes, %d remain",
        path,
        consumed,
        len(tail),
    )
    return 0


def _archive_consumed(path: Path, prefix: bytes) -> None:
    """Append consumed bytes to the .old archive, overwriting past the cap."""
    archive = path.with_name(path.name + ".old")
    try:
        archive_size = archive.stat().st_size
    except OSError:
        archive_size = 0
    mode = "ab" if archive_size + len(prefix) <= _ARCHIVE_MAX_BYTES else "wb"
    try:
        with open(archive, mode) as af:
            af.write(prefix)
    except OSError:
        logger.warning("Failed to archive consumed events to %s", archive)


async def read_new_events(
    path: Path, current_offset: int
) -> tuple[list[HookEvent], int]:
    """Read new hook events from events.jsonl starting at current_offset.

    Returns (events, new_offset). On error returns ([], current_offset).
    Detects file truncation and resets offset to 0 automatically.
    """
    try:
        file_size_stat = path.stat().st_size
    except OSError:
        return [], current_offset

    # Fast path: nothing appended since the last read — skip the open entirely.
    if file_size_stat == current_offset:
        return [], current_offset

    events: list[HookEvent] = []
    new_offset = current_offset

    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            await f.seek(0, 2)
            file_size = await f.tell()
            if current_offset > file_size:
                current_offset = 0
                new_offset = 0
            await f.seek(current_offset)

            async for line in f:
                line = line.strip()
                if not line:
                    new_offset = await f.tell()
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed event line (invalid JSON)")
                    new_offset = await f.tell()
                    continue

                try:
                    record = parse_event_record(data)
                except StateFileValidationError as exc:
                    logger.debug("Skipping invalid event record: %s", exc)
                    new_offset = await f.tell()
                    continue

                events.append(
                    HookEvent(
                        event_type=record.event,
                        window_key=record.window_key,
                        session_id=record.session_id,
                        data=record.data,
                        timestamp=record.ts,
                    )
                )
                new_offset = await f.tell()

    except OSError:
        logger.debug("Could not read events file %s", path)

    return events, new_offset
