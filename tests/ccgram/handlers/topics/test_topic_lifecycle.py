import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Bot
from telegram.error import BadRequest, RetryAfter, TelegramError

from ccgram.destructive_audit import (
    ACTION_TOPIC_RETIRED,
    ACTION_WINDOW_KILLED_TOPIC_GONE,
    ACTION_WINDOW_KILLED_UNBOUND,
    ACTOR_AUTO,
    OUTCOME_SKIPPED_SUSPENDED,
)
from ccgram.window_view import WindowView

from ccgram.handlers.topics.topic_lifecycle import (
    PROBE_MAX_PER_CYCLE,
    _archive_notified,
    check_autoclose_timers,
    check_unbound_window_ttl,
    probe_topic_existence,
    prune_stale_state,
    reset_probe_schedule,
)
from ccgram.handlers.polling.polling_state import (
    lifecycle_strategy,
    terminal_poll_state,
)


@pytest.fixture(autouse=True)
def _clean_strategy_state():
    reset_probe_schedule()
    terminal_poll_state._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()
    _archive_notified.clear()
    yield
    reset_probe_schedule()
    terminal_poll_state._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()
    _archive_notified.clear()


class TestCheckAutocloseTimers:
    async def test_no_topics_is_noop(self):
        bot = AsyncMock(spec=Bot)
        await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()

    async def test_expired_done_topic_gets_closed(self):
        """Default action archives the topic — history must survive."""
        bot = AsyncMock(spec=Bot)
        bot.delete_forum_topic = AsyncMock()
        bot.close_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.safe_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_done_minutes = 1
            mock_config.autoclose_action = "close"
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            await check_autoclose_timers(bot)
        bot.close_forum_topic.assert_called_once()
        bot.delete_forum_topic.assert_not_called()
        mock_send.assert_awaited_once()

    async def test_delete_action_is_opt_in(self):
        """CCGRAM_AUTOCLOSE_ACTION=delete restores the destructive behaviour."""
        bot = AsyncMock(spec=Bot)
        bot.delete_forum_topic = AsyncMock()
        bot.close_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.safe_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_done_minutes = 1
            mock_config.autoclose_action = "delete"
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_called_once()
        bot.close_forum_topic.assert_not_called()
        mock_send.assert_not_awaited()

    async def test_delete_action_falls_back_to_close(self):
        """Legacy fallback survives: delete failure still retires the topic."""
        bot = AsyncMock(spec=Bot)
        bot.delete_forum_topic = AsyncMock(side_effect=BadRequest("no rights"))
        bot.close_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_done_minutes = 1
            mock_config.autoclose_action = "delete"
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            await check_autoclose_timers(bot)
        bot.close_forum_topic.assert_called_once()
        assert lifecycle_strategy.get_state(user_id, thread_id).autoclose is None

    async def test_close_failure_keeps_timer_armed(self):
        """A failed close must not clear the timer or unbind the topic."""
        bot = AsyncMock(spec=Bot)
        bot.close_forum_topic = AsyncMock(side_effect=BadRequest("no rights"))
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.safe_send",
                new_callable=AsyncMock,
            ),
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_config.autoclose_action = "close"
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            await check_autoclose_timers(bot)
        mock_clear.assert_not_awaited()
        assert lifecycle_strategy.get_state(user_id, thread_id).autoclose is not None

    async def test_retiring_a_topic_is_audited(self):
        """Unattended destruction must reach the audit sink, not just the log."""
        bot = AsyncMock(spec=Bot)
        bot.close_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "dead", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.safe_send",
                new_callable=AsyncMock,
            ),
            patch(
                "ccgram.handlers.topics.topic_lifecycle.record_destructive",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_dead_minutes = 1
            mock_config.autoclose_action = "close"
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await check_autoclose_timers(bot)

        mock_audit.assert_awaited_once()
        call = mock_audit.await_args
        assert call is not None
        assert call.args[0] == ACTION_TOPIC_RETIRED
        assert call.kwargs["actor"] == ACTOR_AUTO
        assert call.kwargs["thread_id"] == thread_id

    async def test_archive_notice_not_repeated_across_retries(self):
        """A topic that keeps failing to close is notified exactly once."""
        bot = AsyncMock(spec=Bot)
        bot.close_forum_topic = AsyncMock(side_effect=BadRequest("no rights"))
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.safe_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_done_minutes = 1
            mock_config.autoclose_action = "close"
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            await check_autoclose_timers(bot)
            await check_autoclose_timers(bot)
            await check_autoclose_timers(bot)
        assert bot.close_forum_topic.await_count == 3
        mock_send.assert_awaited_once()

    async def test_not_yet_expired_topic_stays(self):
        bot = AsyncMock(spec=Bot)
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic()
        )
        with patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config:
            mock_config.autoclose_done_minutes = 60
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()

    async def test_expired_dead_topic_stays_when_window_is_live(self):
        bot = AsyncMock(spec=Bot)
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "dead", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
        ):
            mock_config.autoclose_done_minutes = 30
            mock_config.autoclose_dead_minutes = 10
            mock_router.get_window_for_thread.return_value = "@0"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()
        assert lifecycle_strategy.get_state(user_id, thread_id).autoclose is None


def _window_view(origin: str) -> WindowView:
    return WindowView(
        window_id="@0",
        cwd="/tmp",
        provider_name="claude",
        approval_mode="normal",
        batch_mode="batched",
        tool_call_visibility="default",
        transcript_path=None,
        window_name="test",
        session_id="s1",
        origin=origin,
    )


class TestCheckUnboundWindowTtl:
    async def test_no_timeout_is_noop(self):
        with patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config:
            mock_config.autoclose_done_minutes = 0
            await check_unbound_window_ttl([])

    async def test_bound_window_timer_cleared(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await check_unbound_window_ttl([mock_window])
        assert ws.unbound_timer is None

    async def test_manual_unbound_window_is_not_killed(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = []
            mock_wq.view_window.return_value = _window_view("manual_discovered")
            mock_tmux.kill_window = AsyncMock()
            await check_unbound_window_ttl([mock_window])
        assert ws.unbound_timer is None
        mock_tmux.kill_window.assert_not_called()

    async def test_ccgram_created_unbound_window_is_killed_after_ttl(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.record_destructive",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = []
            mock_wq.view_window.return_value = _window_view("ccgram_created")
            mock_tmux.kill_window = AsyncMock()
            await check_unbound_window_ttl([mock_window])
        mock_tmux.kill_window.assert_called_once_with("@0")
        # Killing an agent process is the most destructive thing ccgram does
        # unattended — it must never happen without an audit record.
        mock_audit.assert_awaited_once()
        call = mock_audit.await_args
        assert call is not None
        assert call.args[0] == ACTION_WINDOW_KILLED_UNBOUND
        assert call.kwargs["actor"] == ACTOR_AUTO


class TestBreakerSuspendsDestruction:
    """While the mass-death breaker is tripped, nothing gets destroyed."""

    async def test_autoclose_stands_down_and_keeps_the_timer(self):
        bot = AsyncMock(spec=Bot)
        bot.close_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "dead", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.destruction_blocked",
                return_value="mass_death",
            ),
            patch(
                "ccgram.handlers.topics.topic_lifecycle.record_destructive",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_config.autoclose_dead_minutes = 1
            mock_router.get_window_for_thread.return_value = "@0"
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await check_autoclose_timers(bot)

        bot.close_forum_topic.assert_not_called()
        bot.delete_forum_topic.assert_not_called()
        # Timer must stay armed so the sweep re-runs once the breaker recovers.
        assert lifecycle_strategy.get_state(user_id, thread_id).autoclose is not None
        call = mock_audit.await_args
        assert call is not None
        assert call.kwargs["outcome"] == OUTCOME_SKIPPED_SUSPENDED

    async def test_unbound_ttl_kill_stands_down(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.lifecycle_state"
            ) as mock_lifecycle,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.destruction_blocked",
                return_value="mass_death",
            ),
            patch(
                "ccgram.handlers.topics.topic_lifecycle.record_destructive",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = []
            mock_lifecycle.is_user_detached.return_value = False
            mock_wq.view_window.return_value = _window_view("ccgram_created")
            mock_tmux.kill_window = AsyncMock()
            await check_unbound_window_ttl([mock_window])

        mock_tmux.kill_window.assert_not_called()
        call = mock_audit.await_args
        assert call is not None
        assert call.kwargs["outcome"] == OUTCOME_SKIPPED_SUSPENDED


class TestUserDetachedExemption:
    """/unbind promises the session keeps running — the TTL must honour that."""

    async def test_user_detached_window_is_never_reaped(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.lifecycle_state"
            ) as mock_lifecycle,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = []
            mock_lifecycle.is_user_detached.return_value = True
            mock_tmux.kill_window = AsyncMock()
            await check_unbound_window_ttl([mock_window])
        mock_tmux.kill_window.assert_not_called()
        assert ws.unbound_timer is None

    async def test_rebinding_clears_the_exemption(self):
        """A re-bound window must start a fresh TTL if it is orphaned later."""
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.lifecycle_state"
            ) as mock_lifecycle,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await check_unbound_window_ttl([mock_window])
        mock_lifecycle.set_user_detached.assert_called_once_with("@0", value=False)


class TestHerdrKillPaths:
    """Kill paths route through the multiplexer proxy regardless of window-ID format."""

    async def test_herdr_unbound_window_killed_via_proxy(self):
        """herdr-format window ID w1:t1 is passed through the proxy to kill_window."""
        herdr_id = "w1:t1"
        ws = terminal_poll_state.get_state(herdr_id)
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id=herdr_id, window_name="workspace ▸ agent")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = []
            mock_wq.view_window.return_value = WindowView(
                window_id=herdr_id,
                cwd="/workspace",
                provider_name="claude",
                approval_mode="normal",
                batch_mode="batched",
                tool_call_visibility="default",
                transcript_path=None,
                window_name="workspace ▸ agent",
                session_id="s1",
                origin="ccgram_created",
            )
            mock_tmux.kill_window = AsyncMock()
            await check_unbound_window_ttl([mock_window])
        mock_tmux.kill_window.assert_called_once_with(herdr_id)

    async def test_herdr_deleted_topic_kills_window_via_proxy(self):
        """probe_topic_existence kills the herdr window via proxy on topic deletion."""
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Topic_id_invalid")
        )
        bot.send_message = AsyncMock(side_effect=BadRequest("Thread not found"))
        herdr_id = "w1:t1"
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, herdr_id)]
            mock_router.resolve_chat_id.return_value = -100
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id=herdr_id)
            )
            mock_wq.view_window.return_value = WindowView(
                window_id=herdr_id,
                cwd="/workspace",
                provider_name="claude",
                approval_mode="normal",
                batch_mode="batched",
                tool_call_visibility="default",
                transcript_path=None,
                window_name="workspace ▸ agent",
                session_id="s1",
                origin="ccgram_created",
            )
            mock_tmux.kill_window = AsyncMock()
            await probe_topic_existence(bot)
        mock_tmux.kill_window.assert_called_once_with(herdr_id)
        mock_router.unbind_thread.assert_called_once_with(1, 100)


class TestPruneStaleState:
    async def test_syncs_display_names(self):
        mock_window = MagicMock(window_id="@0", window_name="test")
        with patch("ccgram.handlers.topics.topic_lifecycle.session_manager") as mock_sm:
            await prune_stale_state([mock_window])
            mock_sm.sync_display_names.assert_called_once_with([("@0", "test")])
            mock_sm.prune_stale_state.assert_called_once_with({"@0"})


class TestProbeTopicExistence:
    async def test_flood_control_backs_off_without_suspending(self):
        from ccgram.handlers.topics import topic_lifecycle as tl

        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=[RetryAfter(3), None]
        )
        with (
            patch.object(tl, "thread_router") as mock_router,
            patch.object(tl, "lifecycle_strategy") as mock_strategy,
            patch.object(tl.time, "monotonic", return_value=100.0),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_strategy.should_skip_probe.return_value = False

            await probe_topic_existence(bot)
            mock_strategy.record_probe_failure.assert_not_called()

            bot.unpin_all_forum_topic_messages.reset_mock()
            await probe_topic_existence(bot)
            bot.unpin_all_forum_topic_messages.assert_not_called()

            tl._probe_backoff_until[42] = 0.0
            await probe_topic_existence(bot)
            bot.unpin_all_forum_topic_messages.assert_called_once()

    async def test_probe_budget_allows_only_one_topic_per_chat(self):
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock()
        bindings = [(1, 100 + i, f"@{i}") for i in range(4)]
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.thread_router"
        ) as mock_router:
            mock_router.iter_thread_bindings.return_value = bindings
            mock_router.resolve_chat_id.return_value = 42
            await probe_topic_existence(bot)

        bot.unpin_all_forum_topic_messages.assert_called_once()

    async def test_probe_budget_rotates_across_chats(self):
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock()
        bindings = [(1, 100 + i, f"@{i}") for i in range(3 * PROBE_MAX_PER_CYCLE)]
        chat_by_thread = {100 + i: 1000 + i for i in range(len(bindings))}
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.thread_router"
        ) as mock_router:
            mock_router.iter_thread_bindings.return_value = bindings
            mock_router.resolve_chat_id.side_effect = (
                lambda _user_id, thread_id: chat_by_thread[thread_id]
            )
            probed: list[int] = []
            for _ in range(3):
                bot.unpin_all_forum_topic_messages.reset_mock()
                await probe_topic_existence(bot)
                assert (
                    bot.unpin_all_forum_topic_messages.call_count
                    == PROBE_MAX_PER_CYCLE
                )
                probed.extend(
                    call.kwargs["message_thread_id"]
                    for call in bot.unpin_all_forum_topic_messages.call_args_list
                )

        assert sorted(probed) == [binding[1] for binding in bindings]

    async def test_deleted_topic_unbinds(self):
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Topic_id_invalid")
        )
        # The authoritative send-probe must agree before anything is torn down.
        bot.send_message = AsyncMock(side_effect=BadRequest("Thread not found"))
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_wq.view_window.return_value = _window_view("manual_discovered")
            mock_tmux.kill_window = AsyncMock()
            await probe_topic_existence(bot)
            mock_router.unbind_thread.assert_called_once_with(1, 100)
            mock_tmux.kill_window.assert_not_called()

    async def test_unconfirmed_probe_never_kills(self):
        """The unpin sweep is a ping, not a verdict — a hiccup must not destroy."""
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Topic_id_invalid")
        )
        # Authoritative probe succeeds → the topic is alive after all.
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.kill_window = AsyncMock()
            await probe_topic_existence(bot)

        mock_tmux.kill_window.assert_not_called()
        mock_router.unbind_thread.assert_not_called()
        mock_clear.assert_not_awaited()
        # The invisible probe message must not be left behind in the topic.
        bot.delete_message.assert_awaited_once_with(42, 7)

    async def test_inconclusive_network_error_never_kills(self):
        """A transient failure on the confirmation probe is not a verdict."""
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Topic_id_invalid")
        )
        bot.send_message = AsyncMock(side_effect=TelegramError("network down"))
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.kill_window = AsyncMock()
            await probe_topic_existence(bot)

        mock_tmux.kill_window.assert_not_called()
        mock_router.unbind_thread.assert_not_called()

    async def test_confirmed_deletion_kills_and_audits(self):
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Topic_id_invalid")
        )
        bot.send_message = AsyncMock(side_effect=BadRequest("Thread not found"))
        with (
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.record_destructive",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_wq.view_window.return_value = _window_view("ccgram_created")
            mock_tmux.kill_window = AsyncMock()
            await probe_topic_existence(bot)

        mock_tmux.kill_window.assert_called_once_with("@0")
        mock_audit.assert_awaited_once()
        call = mock_audit.await_args
        assert call is not None
        assert call.args[0] == ACTION_WINDOW_KILLED_TOPIC_GONE

    async def test_suspended_probe_skipped(self):
        bot = AsyncMock(spec=Bot)
        ws = terminal_poll_state.get_state("@0")
        ws.probe_failures = 999
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.thread_router"
        ) as mock_router:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await probe_topic_existence(bot)
        bot.unpin_all_forum_topic_messages.assert_not_called()

    async def test_missing_pin_rights_disables_probe_without_suspending(self):
        from ccgram.handlers.topics import topic_lifecycle as tl

        bot = AsyncMock(spec=Bot)
        # Real Telegram error for unpin without can_pin_messages is lowercase.
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("not enough rights to manage pinned messages")
        )
        wid = "@probe-pin"
        tl._probe_pin_disabled.discard(wid)
        try:
            with (
                patch.object(tl, "thread_router") as mock_router,
                patch.object(tl, "lifecycle_strategy") as mock_strategy,
            ):
                mock_router.iter_thread_bindings.return_value = [(1, 100, wid)]
                mock_router.resolve_chat_id.return_value = 42
                mock_strategy.should_skip_probe.return_value = False

                await probe_topic_existence(bot)

                # Permission error must not count as a probe failure (no suspend).
                mock_strategy.record_probe_failure.assert_not_called()
                assert wid in tl._probe_pin_disabled

                # Next tick skips the window entirely — no further API call.
                bot.unpin_all_forum_topic_messages.reset_mock()
                await probe_topic_existence(bot)
                bot.unpin_all_forum_topic_messages.assert_not_called()
        finally:
            tl._probe_pin_disabled.discard(wid)
