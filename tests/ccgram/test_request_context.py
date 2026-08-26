from ccgram.request_context import record_request, reply_message_id, reset_for_testing


def test_reply_target_is_scoped_to_window_user_and_topic() -> None:
    reset_for_testing()
    record_request("@1", user_id=10, chat_id=-100, thread_id=7, message_id=99)

    assert reply_message_id("@1", user_id=10, thread_id=7) == 99
    assert reply_message_id("@1", user_id=20, thread_id=7) is None
    assert reply_message_id("@1", user_id=10, thread_id=8) is None
    assert reply_message_id("@2", user_id=10, thread_id=7) is None


def test_continuation_preserves_root_question() -> None:
    reset_for_testing()
    record_request("@1", user_id=10, chat_id=-100, thread_id=7, message_id=99)
    record_request(
        "@1",
        user_id=10,
        chat_id=-100,
        thread_id=7,
        message_id=100,
        preserve_existing=True,
    )

    assert reply_message_id("@1", user_id=10, thread_id=7) == 99
