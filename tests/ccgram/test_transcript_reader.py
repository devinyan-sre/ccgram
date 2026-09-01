"""Tests for transcript reader offset handling."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ccgram.idle_tracker import IdleTracker
from ccgram.monitor_state import MonitorState, TrackedSession
from ccgram.providers.base import AgentMessage, MessageRole
from ccgram.providers.codex import CodexProvider
from ccgram.transcript_reader import TranscriptReader, _timestamp_recovery_offset


def _agent_message(
    text: str,
    *,
    role: MessageRole = "assistant",
    is_complete: bool = True,
) -> AgentMessage:
    return AgentMessage(
        text=text,
        role=role,
        content_type="text",
        is_complete=is_complete,
    )


@pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini", "pi"])
def test_complete_assistant_text_is_deduplicated_within_user_turn(
    tmp_path, provider_name: str
) -> None:
    reader = TranscriptReader(
        MonitorState(state_file=tmp_path / "monitor_state.json"), IdleTracker()
    )

    first = reader._deduplicate_complete_assistant_text(
        "session", provider_name, [_agent_message("same answer")]
    )
    duplicate = reader._deduplicate_complete_assistant_text(
        "session", provider_name, [_agent_message("same answer")]
    )

    assert [message.text for message in first] == ["same answer"]
    assert duplicate == []


def test_user_turn_allows_same_assistant_text_again(tmp_path) -> None:
    reader = TranscriptReader(
        MonitorState(state_file=tmp_path / "monitor_state.json"), IdleTracker()
    )
    reader._deduplicate_complete_assistant_text(
        "session", "claude", [_agent_message("same answer")]
    )

    messages = reader._deduplicate_complete_assistant_text(
        "session",
        "claude",
        [
            _agent_message("repeat it", role="user"),
            _agent_message("same answer"),
        ],
    )

    assert [message.text for message in messages] == ["repeat it", "same answer"]


def test_stream_snapshot_does_not_hide_matching_final_text(tmp_path) -> None:
    reader = TranscriptReader(
        MonitorState(state_file=tmp_path / "monitor_state.json"), IdleTracker()
    )

    stream = reader._deduplicate_complete_assistant_text(
        "session", "codex", [_agent_message("answer", is_complete=False)]
    )
    final = reader._deduplicate_complete_assistant_text(
        "session", "codex", [_agent_message("answer")]
    )

    assert [message.is_complete for message in stream] == [False]
    assert [message.is_complete for message in final] == [True]


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


async def test_new_tracker_replays_active_task_from_persisted_dispatch_offset(
    tmp_path,
) -> None:
    old = '{"timestamp":"2026-09-01T12:00:00Z","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"old"}}\n'
    user = '{"timestamp":"2026-09-01T12:10:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"question"}]}}\n'
    final = '{"timestamp":"2026-09-01T12:11:00Z","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"finished"}}\n'
    session_file = tmp_path / "rollout-active.jsonl"
    session_file.write_text(old + user + final)
    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    reader = TranscriptReader(state, IdleTracker())
    dispatch = SimpleNamespace(
        transcript_offset=len(old.encode()),
        created_at=0.0,
        task_id="T0044",
    )
    observer = MagicMock()

    with (
        patch(
            "ccgram.transcript_reader._resolve_provider_for_file",
            return_value=CodexProvider(),
        ),
        patch(
            "ccgram.dispatch_confirmation.dispatch_confirmation.record_for_window",
            return_value=dispatch,
        ),
        patch(
            "ccgram.dispatch_confirmation.dispatch_confirmation.observe_transcript_entries",
            observer,
        ),
    ):
        first: list = []
        await reader._process_session_file(
            "active", session_file, first, window_id="@1"
        )
        recovered: list = []
        await reader._process_session_file(
            "active", session_file, recovered, window_id="@1"
        )

    assert first == []
    assert [message.text for message in recovered] == ["question", "finished"]
    assert recovered[-1].is_complete is True
    assert recovered[-1].phase == "final_answer"
    observer.assert_called_once()


def test_legacy_dispatch_finds_timestamp_recovery_boundary(tmp_path) -> None:
    old = '{"timestamp":"2026-09-01T12:00:00Z","type":"event_msg"}\n'
    current = '{"timestamp":"2026-09-01T12:10:00Z","type":"response_item"}\n'
    session_file = tmp_path / "rollout-legacy.jsonl"
    session_file.write_text(old + current)

    offset = _timestamp_recovery_offset(
        session_file,
        since=1788264600.0,  # 2026-09-01T12:10:00Z
        file_size=session_file.stat().st_size,
    )

    assert offset == len(old.encode())


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
