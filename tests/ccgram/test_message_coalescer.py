import asyncio
from unittest.mock import AsyncMock, MagicMock

from ccgram.message_coalescer import MessageCoalescer


async def test_rapid_messages_are_combined_in_order() -> None:
    coalescer = MessageCoalescer()
    update = MagicMock()
    update.effective_message = AsyncMock()
    callback = AsyncMock()

    await coalescer.submit(
        key=(-100, 7, 10),
        update=update,
        context=object(),
        text="first",
        delay_ms=10,
        callback=callback,
    )
    await coalescer.submit(
        key=(-100, 7, 10),
        update=update,
        context=object(),
        text="second",
        delay_ms=10,
        callback=callback,
    )
    await asyncio.sleep(0.03)

    callback.assert_awaited_once()
    awaited = callback.await_args
    assert awaited is not None
    assert awaited.args[2] == "first\n\nsecond"
    update.effective_message.reply_text.assert_awaited_once()
