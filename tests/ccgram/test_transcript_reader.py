"""Tests for transcript reader offset handling."""

import os

from ccgram.idle_tracker import IdleTracker
from ccgram.monitor_state import MonitorState, TrackedSession
from ccgram.transcript_reader import TranscriptReader


async def test_same_transcript_reuses_offset_after_session_map_refresh(
    tmp_path,
) -> None:
    """A tmux rename/session-map refresh must not replay an existing transcript."""
    first = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"old"}]}}\n'
    )
    second = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"new"}]}}\n'
    )
    session_file = tmp_path / "transcript.jsonl"
    session_file.write_text(first + second, newline="\n")

    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    state.update_session(
        TrackedSession(
            session_id="sess-before-rename",
            file_path=str(session_file),
            last_byte_offset=len(first.encode()),
        )
    )
    reader = TranscriptReader(state, IdleTracker())

    messages = []
    await reader._process_session_file(
        "sess-after-rename",
        session_file,
        messages,
        window_id="@1",
    )

    assert [msg.text for msg in messages] == ["new"]
    tracked = state.get_session("sess-after-rename")
    assert tracked is not None
    assert tracked.last_byte_offset == session_file.stat().st_size


async def test_atomic_replacement_is_read_from_zero(tmp_path) -> None:
    old = '{"type":"assistant","message":{"content":[{"type":"text","text":"old"}]}}\n'
    fresh = old.replace("old", "fresh")
    session_file = tmp_path / "transcript.jsonl"
    session_file.write_text(old)
    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    state.update_session(TrackedSession("sess", str(session_file), 0))
    reader = TranscriptReader(state, IdleTracker())
    first: list = []
    await reader._process_session_file("sess", session_file, first, window_id="@1")

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(fresh)
    replacement.replace(session_file)
    messages: list = []
    await reader._process_session_file("sess", session_file, messages, window_id="@1")

    assert [message.text for message in messages] == ["fresh"]
    assert first[0].delivery_id != messages[0].delivery_id


async def test_same_inode_rewrite_with_preserved_mtime_resets_offset(tmp_path) -> None:
    old = '{"type":"assistant","message":{"content":[{"type":"text","text":"old"}]}}\n'
    new = old.replace("old", "new")
    session_file = tmp_path / "transcript.jsonl"
    session_file.write_text(old)
    initial_stat = session_file.stat()
    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    state.update_session(TrackedSession("sess", str(session_file), 0))
    reader = TranscriptReader(state, IdleTracker())
    await reader._process_session_file("sess", session_file, [], window_id="@1")

    session_file.write_text(new)
    os.utime(session_file, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
    messages: list = []
    await reader._process_session_file("sess", session_file, messages, window_id="@1")

    assert [message.text for message in messages] == ["new"]


async def test_metadata_only_ctime_bump_does_not_replay(tmp_path) -> None:
    history = "".join(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"h%d"}]}}\n'
        % index
        for index in range(5)
    )
    session_file = tmp_path / "transcript.jsonl"
    session_file.write_text(history)
    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    state.update_session(
        TrackedSession("sess", str(session_file), session_file.stat().st_size)
    )
    reader = TranscriptReader(state, IdleTracker())

    os.chmod(session_file, 0o600)
    messages: list = []
    await reader._process_session_file("sess", session_file, messages, window_id="@1")

    assert messages == []


async def test_append_during_read_does_not_replay_history(
    tmp_path, monkeypatch
) -> None:
    history = "".join(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"h%d"}]}}\n'
        % index
        for index in range(5)
    )
    fresh = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"fresh"}]}}\n'
    )
    session_file = tmp_path / "transcript.jsonl"
    session_file.write_text(history)
    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    state.update_session(
        TrackedSession("sess", str(session_file), session_file.stat().st_size)
    )
    reader = TranscriptReader(state, IdleTracker())
    original_read = reader._read_new_lines
    appended = False

    async def append_then_read(*args, **kwargs):
        nonlocal appended
        if not appended:
            with session_file.open("a") as transcript:
                transcript.write(fresh)
            appended = True
        return await original_read(*args, **kwargs)

    monkeypatch.setattr(reader, "_read_new_lines", append_then_read)
    messages: list = []
    await reader._process_session_file("sess", session_file, messages, window_id="@1")

    assert [message.text for message in messages] == ["fresh"]


async def test_rewrite_during_read_retries_new_generation(
    tmp_path, monkeypatch
) -> None:
    history = "".join(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"h%d"}]}}\n'
        % index
        for index in range(5)
    )
    session_file = tmp_path / "transcript.jsonl"
    session_file.write_text(history)
    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    state.update_session(
        TrackedSession("sess", str(session_file), session_file.stat().st_size)
    )
    reader = TranscriptReader(state, IdleTracker())
    original_read = reader._read_new_lines
    rewritten = False

    async def rewrite_then_read(*args, **kwargs):
        nonlocal rewritten
        if not rewritten:
            session_file.write_text(
                session_file.read_text().replace('"h0"', '"changed"', 1)
            )
            rewritten = True
        return await original_read(*args, **kwargs)

    reader._file_mtimes["sess"] = 0.0
    monkeypatch.setattr(reader, "_read_new_lines", rewrite_then_read)
    messages: list = []
    await reader._process_session_file("sess", session_file, messages, window_id="@1")

    assert [message.text for message in messages] == [
        "changed",
        "h1",
        "h2",
        "h3",
        "h4",
    ]
