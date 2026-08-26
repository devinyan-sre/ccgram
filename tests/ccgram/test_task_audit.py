import json
import os
import time
from unittest.mock import patch

from ccgram.task_audit import cancellation_summary, record_task_audit


def test_task_audit_is_private_durable_jsonl(tmp_path) -> None:
    path = tmp_path / "task-audit.jsonl"
    with patch("ccgram.task_audit.config.task_audit_file", path):
        record_task_audit(
            "cancel_confirmed",
            task_id="T0007",
            requester_user_id=10,
            owner_user_id=10,
            chat_id=-100,
            thread_id=7,
            window_id="@1",
        )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["action"] == "cancel_confirmed"
    assert row["task_id"] == "T0007"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_cancellation_summary_only_counts_recent_events(tmp_path) -> None:
    path = tmp_path / "task-audit.jsonl"
    rows = [
        {"timestamp": time.time(), "action": "cancel_confirmed"},
        {"timestamp": time.time() - 172800, "action": "force_cancelled"},
        {"timestamp": time.time(), "action": "cancel_timeout"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    with patch("ccgram.task_audit.config.task_audit_file", path):
        summary = cancellation_summary(hours=24)

    assert summary == {"cancel_confirmed": 1, "cancel_timeout": 1}


def test_cancellation_summary_includes_rotated_file(tmp_path) -> None:
    path = tmp_path / "task-audit.jsonl"
    rotated = tmp_path / "task-audit.jsonl.1"
    path.write_text(
        json.dumps({"timestamp": time.time(), "action": "cancel_confirmed"}),
        encoding="utf-8",
    )
    rotated.write_text(
        json.dumps({"timestamp": time.time(), "action": "force_cancelled"}),
        encoding="utf-8",
    )

    with patch("ccgram.task_audit.config.task_audit_file", path):
        summary = cancellation_summary(hours=24)

    assert summary == {"force_cancelled": 1, "cancel_confirmed": 1}
