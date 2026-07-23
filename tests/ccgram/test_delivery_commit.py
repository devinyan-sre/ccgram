"""Delivery-committed monitor offsets — outbound-queue crash recovery.

Only bytes whose parsed messages reached a delivery terminal state are
persisted (``TrackedSession.delivered_byte_offset``, written under the
existing ``last_byte_offset`` key). A crash between transcript read and
Telegram send restarts the reader at the delivered cursor and replays the
lost batch (at-least-once).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from ccgram.handlers.messaging_pipeline import message_queue
from ccgram.idle_tracker import IdleTracker
from ccgram.monitor_state import MonitorState, TrackedSession
from ccgram.transcript_reader import TranscriptReader

_MSG = '{"type":"assistant","message":{"content":[{"type":"text","text":"%s"}]}}\n'


class TestTrackedSessionCursors:
    def test_delivered_defaults_to_read_cursor(self) -> None:
        s = TrackedSession(session_id="s", file_path="f", last_byte_offset=42)
        assert s.delivered_byte_offset == 42

    def test_explicit_delivered_kept(self) -> None:
        s = TrackedSession(
            session_id="s", file_path="f", last_byte_offset=42, delivered_byte_offset=10
        )
        assert s.delivered_byte_offset == 10

    def test_to_dict_persists_delivered_not_read(self) -> None:
        s = TrackedSession(
            session_id="s", file_path="f", last_byte_offset=42, delivered_byte_offset=10
        )
        assert s.to_dict()["last_byte_offset"] == 10

    def test_to_dict_clamps_to_read_cursor(self) -> None:
        # Truncation reset can leave a stale-high delivered value.
        s = TrackedSession(
            session_id="s", file_path="f", last_byte_offset=5, delivered_byte_offset=99
        )
        assert s.to_dict()["last_byte_offset"] == 5

    def test_roundtrip_restarts_both_cursors_at_delivered(self) -> None:
        s = TrackedSession(
            session_id="s", file_path="f", last_byte_offset=42, delivered_byte_offset=10
        )
        restored = TrackedSession.from_dict(s.to_dict())
        assert restored.last_byte_offset == 10
        assert restored.delivered_byte_offset == 10


def _reader(tmp_path) -> tuple[TranscriptReader, MonitorState]:
    state = MonitorState(state_file=tmp_path / "monitor_state.json")
    return TranscriptReader(state, IdleTracker()), state


async def _track_from_zero(state: MonitorState, session_id: str, path) -> None:
    state.update_session(
        TrackedSession(session_id=session_id, file_path=str(path), last_byte_offset=0)
    )


class TestReaderCommitFlow:
    async def test_batch_with_messages_defers_commit(self, tmp_path) -> None:
        session_file = tmp_path / "t.jsonl"
        session_file.write_text(_MSG % "hello", newline="\n")
        reader, state = _reader(tmp_path)
        await _track_from_zero(state, "s1", session_file)

        messages: list = []
        await reader._process_session_file("s1", session_file, messages)

        assert [m.text for m in messages] == ["hello"]
        tracked = state.get_session("s1")
        assert tracked is not None
        size = session_file.stat().st_size
        assert tracked.last_byte_offset == size  # read cursor advanced
        assert tracked.delivered_byte_offset == 0  # commit deferred
        assert reader._pending_commits == {"s1": size}
        assert tracked.to_dict()["last_byte_offset"] == 0  # crash-safe persist

    async def test_batch_without_messages_commits_immediately(self, tmp_path) -> None:
        session_file = tmp_path / "t.jsonl"
        # A user entry produces no outbound messages.
        session_file.write_text('{"type":"user","message":{}}\n', newline="\n")
        reader, state = _reader(tmp_path)
        await _track_from_zero(state, "s1", session_file)

        messages: list = []
        await reader._process_session_file("s1", session_file, messages)

        assert messages == []
        tracked = state.get_session("s1")
        assert tracked is not None
        assert tracked.delivered_byte_offset == tracked.last_byte_offset
        assert reader._pending_commits == {}

    async def test_commit_waits_for_drained_queues(self, tmp_path) -> None:
        session_file = tmp_path / "t.jsonl"
        session_file.write_text(_MSG % "hello", newline="\n")
        reader, state = _reader(tmp_path)
        await _track_from_zero(state, "s1", session_file)
        messages: list = []
        await reader._process_session_file("s1", session_file, messages)
        size = session_file.stat().st_size

        reader.commit_delivered(lambda _sid: False)  # queues busy
        tracked = state.get_session("s1")
        assert tracked is not None
        assert tracked.delivered_byte_offset == 0
        assert reader._pending_commits == {"s1": size}

        reader.commit_delivered(lambda _sid: True)  # queues drained
        assert tracked.delivered_byte_offset == size
        assert reader._pending_commits == {}
        assert tracked.to_dict()["last_byte_offset"] == size

    async def test_commit_without_callback_is_unconditional(self, tmp_path) -> None:
        session_file = tmp_path / "t.jsonl"
        session_file.write_text(_MSG % "hello", newline="\n")
        reader, state = _reader(tmp_path)
        await _track_from_zero(state, "s1", session_file)
        await reader._process_session_file("s1", session_file, [])

        reader.commit_delivered(None)
        tracked = state.get_session("s1")
        assert tracked is not None
        assert tracked.delivered_byte_offset == session_file.stat().st_size

    async def test_clear_session_drops_pending_commit(self, tmp_path) -> None:
        session_file = tmp_path / "t.jsonl"
        session_file.write_text(_MSG % "hello", newline="\n")
        reader, state = _reader(tmp_path)
        await _track_from_zero(state, "s1", session_file)
        await reader._process_session_file("s1", session_file, [])
        assert "s1" in reader._pending_commits

        reader.clear_session("s1")
        assert reader._pending_commits == {}

    async def test_adoption_carries_delivered_cursor_and_pending(
        self, tmp_path
    ) -> None:
        first = _MSG % "old"
        session_file = tmp_path / "t.jsonl"
        session_file.write_text(first + _MSG % "new", newline="\n")
        reader, state = _reader(tmp_path)
        state.update_session(
            TrackedSession(
                session_id="old-id",
                file_path=str(session_file),
                last_byte_offset=len(first.encode()),
                delivered_byte_offset=0,
            )
        )
        reader._pending_commits["old-id"] = len(first.encode())

        messages: list = []
        await reader._process_session_file("new-id", session_file, messages)

        assert [m.text for m in messages] == ["new"]
        tracked = state.get_session("new-id")
        assert tracked is not None
        assert tracked.delivered_byte_offset == 0  # carried, not reset
        assert "old-id" not in reader._pending_commits
        assert reader._pending_commits["new-id"] == session_file.stat().st_size

    async def test_crash_replay_resends_undelivered_batch(self, tmp_path) -> None:
        """End-to-end: crash before delivery → restart replays the batch."""
        session_file = tmp_path / "t.jsonl"
        session_file.write_text(_MSG % "lost on crash", newline="\n")
        reader, state = _reader(tmp_path)
        await _track_from_zero(state, "s1", session_file)
        messages: list = []
        await reader._process_session_file("s1", session_file, messages)
        assert [m.text for m in messages] == ["lost on crash"]
        # Crash before commit_delivered: persisted state has delivered=0.
        tracked = state.get_session("s1")
        assert tracked is not None
        persisted = tracked.to_dict()

        # Restart: fresh reader/state from the persisted record.
        state2 = MonitorState(state_file=tmp_path / "monitor_state.json")
        state2.update_session(TrackedSession.from_dict(persisted))
        reader2 = TranscriptReader(state2, IdleTracker())
        replayed: list = []
        await reader2._process_session_file("s1", session_file, replayed)
        assert [m.text for m in replayed] == ["lost on crash"]

    async def test_delivered_batch_not_replayed_after_restart(self, tmp_path) -> None:
        session_file = tmp_path / "t.jsonl"
        session_file.write_text(_MSG % "delivered", newline="\n")
        reader, state = _reader(tmp_path)
        await _track_from_zero(state, "s1", session_file)
        await reader._process_session_file("s1", session_file, [])
        reader.commit_delivered(lambda _sid: True)
        tracked = state.get_session("s1")
        assert tracked is not None
        persisted = tracked.to_dict()

        state2 = MonitorState(state_file=tmp_path / "monitor_state.json")
        state2.update_session(TrackedSession.from_dict(persisted))
        reader2 = TranscriptReader(state2, IdleTracker())
        replayed: list = []
        await reader2._process_session_file("s1", session_file, replayed)
        assert replayed == []


class TestIsSessionDeliveryDrained:
    def _users(self, *user_ids: int):
        return [(uid, "@0", 100) for uid in user_ids]

    def test_no_users_is_drained(self) -> None:
        with patch("ccgram.session_query.find_users_for_session", return_value=[]):
            assert message_queue.is_session_delivery_drained("s1") is True

    def test_no_queue_is_drained(self) -> None:
        with patch(
            "ccgram.session_query.find_users_for_session",
            return_value=self._users(999999),
        ):
            assert message_queue.is_session_delivery_drained("s1") is True

    async def test_pending_queue_not_drained(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(object())
        with (
            patch.dict(message_queue._message_queues, {7: queue}),
            patch(
                "ccgram.session_query.find_users_for_session",
                return_value=self._users(7),
            ),
        ):
            assert message_queue.is_session_delivery_drained("s1") is False

    async def test_in_flight_task_not_drained(self) -> None:
        # Task popped by the worker but not yet task_done(): empty() is True
        # but join-accounting still shows one unfinished task.
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(object())
        queue.get_nowait()
        assert queue.empty()
        with (
            patch.dict(message_queue._message_queues, {7: queue}),
            patch(
                "ccgram.session_query.find_users_for_session",
                return_value=self._users(7),
            ),
        ):
            assert message_queue.is_session_delivery_drained("s1") is False

    async def test_fully_drained(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(object())
        queue.get_nowait()
        queue.task_done()
        with (
            patch.dict(message_queue._message_queues, {7: queue}),
            patch(
                "ccgram.session_query.find_users_for_session",
                return_value=self._users(7),
            ),
        ):
            assert message_queue.is_session_delivery_drained("s1") is True
