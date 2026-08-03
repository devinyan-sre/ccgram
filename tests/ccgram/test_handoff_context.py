"""Cross-provider context transfer tests."""

from unittest.mock import AsyncMock, patch

from ccgram.handoff_context import _local_digest, generate_handoff_context


def test_local_digest_keeps_recent_user_and_assistant_text() -> None:
    digest = _local_digest(
        [
            {"role": "user", "content_type": "text", "text": "fix auth"},
            {"role": "assistant", "content_type": "tool", "text": "ignored"},
            {"role": "assistant", "content_type": "text", "text": "tests pass"},
        ]
    )
    assert "User: fix auth" in digest
    assert "Assistant: tests pass" in digest
    assert "ignored" not in digest


async def test_generate_handoff_context_falls_back_without_llm() -> None:
    messages = [
        {"role": "user", "content_type": "text", "text": "fix auth"},
        {"role": "assistant", "content_type": "text", "text": "done"},
    ]
    with (
        patch(
            "ccgram.handoff_context.session_query.get_recent_messages",
            new=AsyncMock(return_value=(messages, 2)),
        ),
        patch("ccgram.llm.get_text_completer", return_value=None),
    ):
        prompt = await generate_handoff_context("@7")
    assert prompt.startswith("Continue this task")
    assert "fix auth" in prompt
    assert "done" in prompt


async def test_generate_handoff_context_uses_configured_summarizer() -> None:
    completer = AsyncMock()
    completer.complete.return_value = "Fixed auth; tests pass; deploy next."
    with (
        patch(
            "ccgram.handoff_context.session_query.get_recent_messages",
            new=AsyncMock(
                return_value=(
                    [{"role": "user", "content_type": "text", "text": "fix auth"}],
                    1,
                )
            ),
        ),
        patch("ccgram.llm.get_text_completer", return_value=completer),
    ):
        prompt = await generate_handoff_context("@7")
    assert "Fixed auth; tests pass; deploy next." in prompt
    completer.complete.assert_awaited_once()
