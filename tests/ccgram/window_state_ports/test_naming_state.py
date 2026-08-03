from __future__ import annotations

from ccgram.window_state_ports.naming_state import (
    NamingProjection,
    get_naming,
    iter_naming,
    set_auto_named,
)
from ccgram.window_state_store import WindowState, WindowStateStore


def test_get_and_iter_naming_projection(store: WindowStateStore) -> None:
    store.window_states["@1"] = WindowState(
        cwd="/srv/ccgram",
        window_name="ccgram-codex-1",
        provider_name="codex",
        auto_named=True,
    )

    expected = NamingProjection(
        window_id="@1",
        cwd="/srv/ccgram",
        window_name="ccgram-codex-1",
        provider_name="codex",
        auto_named=True,
    )
    assert get_naming("@1") == expected
    assert iter_naming() == [expected]
    assert get_naming("@missing") is None


def test_set_auto_named_persists(
    store: WindowStateStore, save_calls: list[int]
) -> None:
    store.window_states["@1"] = WindowState()
    set_auto_named("@1", value=True)
    assert store.window_states["@1"].auto_named is True
    assert save_calls == [1]
