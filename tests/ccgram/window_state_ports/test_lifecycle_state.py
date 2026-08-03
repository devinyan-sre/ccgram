from __future__ import annotations

import pytest

from ccgram.window_state_ports.lifecycle_state import (
    LifecycleProjection,
    get_lifecycle,
    get_origin,
    is_parked,
    is_user_detached,
    set_parked,
    set_user_detached,
    set_window_origin,
)
from ccgram.window_state_store import WindowState, WindowStateStore


class TestReads:
    def test_get_lifecycle_missing(self, store: WindowStateStore) -> None:
        assert get_lifecycle("@missing") is None

    def test_get_lifecycle_default(self, store: WindowStateStore) -> None:
        store.window_states["@1"] = WindowState()
        proj = get_lifecycle("@1")
        assert proj == LifecycleProjection(
            window_id="@1",
            origin="manual_discovered",
        )

    def test_get_lifecycle_ccgram_created(self, store: WindowStateStore) -> None:
        store.window_states["@1"] = WindowState(origin="ccgram_created")
        proj = get_lifecycle("@1")
        assert proj == LifecycleProjection(
            window_id="@1",
            origin="ccgram_created",
        )

    def test_get_origin_invalid_falls_back(self, store: WindowStateStore) -> None:
        store.window_states["@1"] = WindowState(origin="garbage")
        assert get_origin("@1") == "manual_discovered"

    def test_get_origin_missing(self, store: WindowStateStore) -> None:
        assert get_origin("@missing") == "manual_discovered"


class TestWrites:
    def test_set_window_origin_persists(
        self, store: WindowStateStore, save_calls: list[int]
    ) -> None:
        set_window_origin("@1", "ccgram_created")
        assert store.window_states["@1"].origin == "ccgram_created"
        assert len(save_calls) == 1

    def test_set_window_origin_noop_no_save(
        self, store: WindowStateStore, save_calls: list[int]
    ) -> None:
        set_window_origin("@1", "ccgram_created")
        save_calls.clear()
        set_window_origin("@1", "ccgram_created")
        assert save_calls == []

    def test_set_window_origin_invalid(self, store: WindowStateStore) -> None:
        with pytest.raises(ValueError):
            set_window_origin("@1", "garbage")


class TestUserDetached:
    """/unbind marks a window TTL-exempt; re-binding clears the exemption."""

    def test_defaults_to_false(self, store: WindowStateStore) -> None:
        store.window_states["@1"] = WindowState()
        assert is_user_detached("@1") is False

    def test_missing_window_is_not_detached(self, store: WindowStateStore) -> None:
        assert is_user_detached("@missing") is False

    def test_set_persists_and_projects(
        self, store: WindowStateStore, save_calls: list[int]
    ) -> None:
        store.window_states["@1"] = WindowState()
        set_user_detached("@1", value=True)
        assert store.window_states["@1"].user_detached is True
        assert is_user_detached("@1") is True
        proj = get_lifecycle("@1")
        assert proj is not None and proj.user_detached is True
        assert len(save_calls) == 1

    def test_set_noop_no_save(
        self, store: WindowStateStore, save_calls: list[int]
    ) -> None:
        store.window_states["@1"] = WindowState(user_detached=True)
        save_calls.clear()
        set_user_detached("@1", value=True)
        assert save_calls == []

    def test_clearing_is_persisted(self, store: WindowStateStore) -> None:
        store.window_states["@1"] = WindowState(user_detached=True)
        set_user_detached("@1", value=False)
        assert is_user_detached("@1") is False

    def test_survives_a_serialization_round_trip(self) -> None:
        """The flag must outlive a restart, or the TTL reaps parked sessions."""
        restored = WindowState.from_dict(WindowState(user_detached=True).to_dict())
        assert restored.user_detached is True

    def test_absent_from_dict_when_false(self) -> None:
        assert "user_detached" not in WindowState().to_dict()


class TestParked:
    def test_defaults_to_false(self, store: WindowStateStore) -> None:
        store.window_states["@1"] = WindowState()
        assert is_parked("@1") is False

    def test_set_persists_and_projects(
        self, store: WindowStateStore, save_calls: list[int]
    ) -> None:
        store.window_states["@1"] = WindowState()
        set_parked("@1", value=True)
        assert is_parked("@1") is True
        projection = get_lifecycle("@1")
        assert projection is not None and projection.parked is True
        assert len(save_calls) == 1

    def test_survives_a_serialization_round_trip(self) -> None:
        restored = WindowState.from_dict(WindowState(parked=True).to_dict())
        assert restored.parked is True

    def test_absent_from_dict_when_false(self) -> None:
        assert "parked" not in WindowState().to_dict()


class TestAutomaticNamePersistence:
    def test_survives_a_serialization_round_trip(self) -> None:
        restored = WindowState.from_dict(WindowState(auto_named=True).to_dict())
        assert restored.auto_named is True

    def test_absent_from_dict_when_false(self) -> None:
        assert "auto_named" not in WindowState().to_dict()
