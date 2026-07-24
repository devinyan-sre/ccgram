"""State-file snapshots and corruption recovery.

state.json holds every topic↔window binding. The regression these tests exist
to prevent: a corrupt file degrading to empty state and then being overwritten
by the next save, permanently losing every binding.
"""

import json

from ccgram import state_backup
from ccgram.state_persistence import StatePersistence


def _persistence(tmp_path, data=None):
    path = tmp_path / "state.json"
    if data is not None:
        path.write_text(json.dumps(data))
    return StatePersistence(path, lambda: {}), path


# --- snapshot rotation ---------------------------------------------------


def test_snapshot_of_missing_file_is_a_noop(tmp_path):
    assert state_backup.snapshot(tmp_path / "absent.json") is None


def test_snapshot_copies_the_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"a": 1}')
    snap = state_backup.snapshot(path)
    assert snap is not None
    assert json.loads(snap.read_text()) == {"a": 1}


def test_snapshots_rotate_and_keep_a_bounded_history(tmp_path):
    path = tmp_path / "state.json"
    for i in range(state_backup.KEEP_SNAPSHOTS + 4):
        path.write_text(json.dumps({"n": i}))
        state_backup.snapshot(path)
    assert len(state_backup.list_snapshots(path)) == state_backup.KEEP_SNAPSHOTS


def test_newest_snapshot_is_the_most_recent_content(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"n": 1}')
    state_backup.snapshot(path)
    path.write_text('{"n": 2}')
    state_backup.snapshot(path)
    newest = state_backup.newest_snapshot(path)
    assert newest is not None
    assert json.loads(newest.read_text()) == {"n": 2}


def test_newest_snapshot_is_none_without_history(tmp_path):
    assert state_backup.newest_snapshot(tmp_path / "state.json") is None


# --- corruption handling -------------------------------------------------


def test_load_snapshots_a_good_file(tmp_path):
    persistence, path = _persistence(tmp_path, {"bindings": {"1": "@0"}})
    assert persistence.load() == {"bindings": {"1": "@0"}}
    assert state_backup.newest_snapshot(path) is not None


def test_corrupt_file_is_preserved_not_discarded(tmp_path):
    persistence, path = _persistence(tmp_path)
    path.write_text("{not json")
    persistence.load()
    preserved = state_backup.list_corrupt(path)
    assert preserved, "the damaged file must be kept for inspection"
    assert preserved[0].read_text() == "{not json"


def test_preserved_corrupt_files_are_never_offered_as_snapshots(tmp_path):
    """Regression: a corrupt file listed as a snapshot would be restored back."""
    persistence, path = _persistence(tmp_path)
    path.write_text("{not json")
    persistence.load()
    assert state_backup.list_snapshots(path) == []
    assert state_backup.newest_snapshot(path) is None


def test_corrupt_file_recovers_bindings_from_the_newest_snapshot(tmp_path):
    """The core data-loss regression: corruption must not clear bindings."""
    persistence, path = _persistence(tmp_path, {"bindings": {"1": "@0"}})
    persistence.load()  # takes the known-good snapshot

    path.write_text("{truncated")
    recovered = persistence.load()

    assert recovered == {"bindings": {"1": "@0"}}
    # The live file is repaired too, so the next save cannot clobber it.
    assert json.loads(path.read_text()) == {"bindings": {"1": "@0"}}


def test_corrupt_file_without_snapshot_falls_back_to_empty(tmp_path):
    persistence, path = _persistence(tmp_path)
    path.write_text("{not json")
    assert persistence.load() == {}


def test_missing_file_loads_empty_without_creating_backups(tmp_path):
    persistence, path = _persistence(tmp_path)
    assert persistence.load() == {}
    assert state_backup.list_snapshots(path) == []


def test_unreadable_snapshot_degrades_to_empty_rather_than_raising(tmp_path):
    persistence, path = _persistence(tmp_path, {"bindings": {"1": "@0"}})
    persistence.load()
    # Damage both the live file and its only snapshot.
    snap = state_backup.newest_snapshot(path)
    assert snap is not None
    snap.write_text("{also broken")
    path.write_text("{broken")
    assert persistence.load() == {}


# --- restore -------------------------------------------------------------


def test_restore_from_overwrites_the_live_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"n": 1}')
    snap = state_backup.snapshot(path)
    assert snap is not None
    path.write_text('{"n": 999}')

    assert state_backup.restore_from(snap, path) is True
    assert json.loads(path.read_text()) == {"n": 1}


def test_restore_from_missing_snapshot_reports_failure(tmp_path):
    path = tmp_path / "state.json"
    assert state_backup.restore_from(tmp_path / "nope", path) is False


# --- doctor --restore ----------------------------------------------------


def _patch_config_dir(monkeypatch, tmp_path):
    from ccgram import doctor_cmd

    monkeypatch.setattr(doctor_cmd, "ccgram_dir", lambda: tmp_path)
    monkeypatch.setattr(doctor_cmd, "load_ccgram_env", lambda: None)
    return doctor_cmd


def test_restore_main_reports_failure_when_nothing_to_restore(monkeypatch, tmp_path):
    doctor_cmd = _patch_config_dir(monkeypatch, tmp_path)
    assert doctor_cmd.restore_main() == 1


def test_restore_main_restores_the_newest_snapshot(monkeypatch, tmp_path):
    doctor_cmd = _patch_config_dir(monkeypatch, tmp_path)
    path = tmp_path / "state.json"
    path.write_text('{"bindings": {"1": "@0"}}')
    state_backup.snapshot(path)
    path.write_text('{"bindings": {}}')

    assert doctor_cmd.restore_main() == 0
    assert json.loads(path.read_text()) == {"bindings": {"1": "@0"}}


def test_restore_main_snapshots_current_file_so_restore_is_reversible(
    monkeypatch, tmp_path
):
    doctor_cmd = _patch_config_dir(monkeypatch, tmp_path)
    path = tmp_path / "state.json"
    path.write_text('{"n": 1}')
    state_backup.snapshot(path)
    path.write_text('{"n": 2}')

    before = len(state_backup.list_snapshots(path))
    doctor_cmd.restore_main()
    assert len(state_backup.list_snapshots(path)) > before
