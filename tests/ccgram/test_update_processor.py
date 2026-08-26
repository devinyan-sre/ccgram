import asyncio
from datetime import UTC, datetime

from telegram import Chat, Message, Update, User

from ccgram.update_processor import OperatorUpdateProcessor


def _update(update_id: int, user_id: int) -> Update:
    user = User(id=user_id, first_name=f"u{user_id}", is_bot=False)
    chat = Chat(id=-100, type="supergroup")
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="test",
    )
    return Update(update_id=update_id, message=message)


async def test_same_operator_is_serialized() -> None:
    processor = OperatorUpdateProcessor(8)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        first_entered.set()
        await release_first.wait()

    async def second() -> None:
        second_entered.set()

    task1 = asyncio.create_task(processor.process_update(_update(1, 10), first()))
    await first_entered.wait()
    task2 = asyncio.create_task(processor.process_update(_update(2, 10), second()))
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(task1, task2)
    assert second_entered.is_set()


async def test_different_operators_run_in_parallel() -> None:
    processor = OperatorUpdateProcessor(8)
    both_entered = asyncio.Event()
    entered: set[int] = set()

    async def work(user_id: int) -> None:
        entered.add(user_id)
        if len(entered) == 2:
            both_entered.set()
        await both_entered.wait()

    await asyncio.wait_for(
        asyncio.gather(
            processor.process_update(_update(1, 10), work(10)),
            processor.process_update(_update(2, 20), work(20)),
        ),
        timeout=1,
    )
    assert entered == {10, 20}
