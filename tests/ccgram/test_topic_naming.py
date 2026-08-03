"""Provider-aware topic/window naming tests."""

from unittest.mock import AsyncMock, patch

import pytest

from ccgram.topic_naming import (
    automatic_name_prefix,
    project_slug,
    reserve_topic_name,
)
from ccgram.window_state_store import WindowState, window_store
from ccgram.thread_router import thread_router


@pytest.fixture(autouse=True)
def isolated_names():
    with (
        patch.object(window_store, "window_states", {}),
        patch.object(thread_router, "window_display_names", {}),
        patch("ccgram.topic_naming.tmux_manager") as mux,
    ):
        mux.list_windows = AsyncMock(return_value=[])
        yield


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "pi", "shell"])
async def test_all_supported_providers_get_explicit_first_slot(provider: str) -> None:
    async with reserve_topic_name("/srv/ccgram", provider) as reserved:
        assert reserved.name == f"ccgram-{provider}-1"
        assert reserved.automatic is True


async def test_numbering_is_scoped_to_directory_and_provider() -> None:
    window_store.window_states.update(
        {
            "@1": WindowState(
                cwd="/srv/ccgram",
                provider_name="codex",
                window_name="ccgram-codex-1",
                auto_named=True,
            ),
            "@2": WindowState(
                cwd="/srv/ccgram",
                provider_name="claude",
                window_name="ccgram-claude-1",
                auto_named=True,
            ),
        }
    )

    async with reserve_topic_name("/srv/ccgram", "codex") as reserved:
        assert reserved.name == "ccgram-codex-2"
    async with reserve_topic_name("/srv/ccgram", "gemini") as reserved:
        assert reserved.name == "ccgram-gemini-1"


async def test_parked_topic_keeps_its_slot_reserved() -> None:
    window_store.window_states["@dead"] = WindowState(
        cwd="/srv/ccgram",
        provider_name="codex",
        window_name="ccgram-codex-1",
        auto_named=True,
        parked=True,
    )

    async with reserve_topic_name("/srv/ccgram", "codex") as reserved:
        assert reserved.name == "ccgram-codex-2"


async def test_same_provider_wake_reuses_automatic_name() -> None:
    window_store.window_states["@old"] = WindowState(
        cwd="/srv/ccgram",
        provider_name="codex",
        window_name="ccgram-codex-2",
        auto_named=True,
        parked=True,
    )

    async with reserve_topic_name(
        "/srv/ccgram", "codex", replacing_window_id="@old"
    ) as reserved:
        assert reserved.name == "ccgram-codex-2"


async def test_herdr_workspace_prefix_does_not_break_slots_or_wake() -> None:
    window_store.window_states["w1:t1"] = WindowState(
        cwd="/srv/ccgram",
        provider_name="codex",
        window_name="workspace ▸ ccgram-codex-1",
        auto_named=True,
        parked=True,
    )

    async with reserve_topic_name("/srv/ccgram", "codex") as second:
        assert second.name == "ccgram-codex-2"
    async with reserve_topic_name(
        "/srv/ccgram", "codex", replacing_window_id="w1:t1"
    ) as wake:
        assert wake.name == "ccgram-codex-1"


async def test_handoff_allocates_target_provider_slot() -> None:
    window_store.window_states.update(
        {
            "@old": WindowState(
                cwd="/srv/ccgram",
                provider_name="claude",
                window_name="ccgram-claude-1",
                auto_named=True,
            ),
            "@codex": WindowState(
                cwd="/srv/ccgram",
                provider_name="codex",
                window_name="ccgram-codex-1",
                auto_named=True,
            ),
        }
    )

    async with reserve_topic_name(
        "/srv/ccgram", "codex", replacing_window_id="@old"
    ) as reserved:
        assert reserved.name == "ccgram-codex-2"


async def test_manual_name_is_preserved_until_forced() -> None:
    window_store.window_states["@old"] = WindowState(
        cwd="/srv/ccgram",
        provider_name="claude",
        window_name="release-war-room",
        auto_named=False,
    )

    async with reserve_topic_name(
        "/srv/ccgram", "codex", replacing_window_id="@old"
    ) as reserved:
        assert reserved.name == "release-war-room"
        assert reserved.automatic is False

    async with reserve_topic_name(
        "/srv/ccgram",
        "codex",
        replacing_window_id="@old",
        force_automatic=True,
    ) as reserved:
        assert reserved.name == "ccgram-codex-1"
        assert reserved.automatic is True


async def test_legacy_directory_suffix_requires_explicit_migration() -> None:
    window_store.window_states["@old"] = WindowState(
        cwd="/srv/ccgram",
        provider_name="codex",
        window_name="ccgram-3",
    )

    async with reserve_topic_name(
        "/srv/ccgram", "codex", replacing_window_id="@old"
    ) as reserved:
        assert reserved.name == "ccgram-3"
        assert reserved.automatic is False

    async with reserve_topic_name(
        "/srv/ccgram",
        "codex",
        replacing_window_id="@old",
        force_automatic=True,
    ) as reserved:
        assert reserved.name == "ccgram-codex-1"
        assert reserved.automatic is True


async def test_concurrent_reservations_cannot_choose_same_name() -> None:
    async with (
        reserve_topic_name("/srv/ccgram", "codex") as first,
        reserve_topic_name("/srv/ccgram", "codex") as second,
    ):
        assert first.name == "ccgram-codex-1"
        assert second.name == "ccgram-codex-2"


def test_project_slug_is_safe_and_bounded() -> None:
    assert automatic_name_prefix("/srv/My Project", "CODEX") == "My-Project-codex"
    assert len(project_slug("/srv/" + "x" * 200)) <= 72
