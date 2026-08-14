"""Shared defaults for topic-handler tests."""

from unittest.mock import AsyncMock, patch

import pytest

from ccgram.provider_readiness import ProviderReadiness


@pytest.fixture(autouse=True)
def _provider_is_ready_by_default():
    """Keep legacy launch tests focused on their own orchestration concern."""
    with patch(
        "ccgram.handlers.topics.window_launch_service.wait_for_provider_ready",
        new_callable=AsyncMock,
        return_value=ProviderReadiness(True),
    ):
        yield
