"""Tests for daily-digest text analysis.

The behaviour that matters here is telling conversation apart from tool
traffic: before this module, the digest counted every ``type="user"`` entry
as a prompt, so 462 tool results and 25 real questions were reported as
"491 prompts". A digest that misreports activity by 20x is worse than none.
"""

import datetime as dt

from ccgram.handlers.digest_text import (
    DigestStats,
    analyze,
    entry_ts,
    extract_keywords,
    extract_user_prompt,
    is_text_reply,
)


def _user(content) -> dict:
    return {"type": "user", "message": {"content": content}}


def _assistant(content) -> dict:
    return {"type": "assistant", "message": {"content": content}}


class TestExtractUserPrompt:
    def test_plain_prompt(self) -> None:
        assert extract_user_prompt(_user("部署一下服务")) == "部署一下服务"

    def test_tool_result_is_not_a_prompt(self) -> None:
        """The 20x inflation came from here: tool results are `user` entries."""
        entry = _user([{"type": "tool_result", "content": "ok"}])
        assert extract_user_prompt(entry) is None

    def test_task_notification_is_not_a_prompt(self) -> None:
        entry = _user("<task-notification>\n<task-id>x</task-id>\n</task-notification>")
        assert extract_user_prompt(entry) is None

    def test_slash_command_plumbing_is_not_a_prompt(self) -> None:
        assert extract_user_prompt(_user("<command-name>/clear</command-name>")) is None

    def test_system_reminder_is_not_a_prompt(self) -> None:
        assert (
            extract_user_prompt(_user("<system-reminder>note</system-reminder>"))
            is None
        )

    def test_channel_envelope_is_unwrapped(self) -> None:
        """claude-ops wraps real messages; the payload is the prompt."""
        entry = _user(
            '<channel source="plugin:telegram" chat_id="1" user="me">\n'
            "重启一下服务\n</channel>"
        )
        assert extract_user_prompt(entry) == "重启一下服务"

    def test_reply_quote_keeps_only_the_new_text(self) -> None:
        """The quoted block is previous output — counting it skews themes."""
        entry = _user(
            '[Replying to this earlier message:]\n"""\n旧的部署说明文字\n"""\n\n继续处理'
        )
        assert extract_user_prompt(entry) == "继续处理"

    def test_prompt_that_is_only_a_quote_is_dropped(self) -> None:
        entry = _user('[Replying to this earlier message:]\n"""\n旧内容\n"""\n')
        assert extract_user_prompt(entry) is None

    def test_assistant_entry_is_not_a_prompt(self) -> None:
        assert extract_user_prompt(_assistant("hi")) is None

    def test_blank_is_dropped(self) -> None:
        assert extract_user_prompt(_user("   ")) is None


class TestIsTextReply:
    def test_text_block_counts(self) -> None:
        assert is_text_reply(_assistant([{"type": "text", "text": "好的"}])) is True

    def test_plain_string_counts(self) -> None:
        assert is_text_reply(_assistant("好的")) is True

    def test_tool_use_only_turn_does_not_count(self) -> None:
        """A pure tool call is machinery — nobody read it as a reply."""
        entry = _assistant([{"type": "tool_use", "name": "Bash", "input": {}}])
        assert is_text_reply(entry) is False

    def test_empty_text_block_does_not_count(self) -> None:
        assert is_text_reply(_assistant([{"type": "text", "text": "  "}])) is False

    def test_user_entry_is_not_a_reply(self) -> None:
        assert is_text_reply(_user("hi")) is False


class TestExtractKeywords:
    def test_recurring_term_surfaces(self) -> None:
        texts = ["部署前端服务", "部署后端服务", "部署完成了吗"]
        assert "部署" in extract_keywords(texts)

    def test_single_mention_is_not_a_theme(self) -> None:
        assert extract_keywords(["只提一次的词"]) == []

    def test_empty_input(self) -> None:
        assert extract_keywords([]) == []

    def test_filler_is_rejected(self) -> None:
        """Grammar words must never win a slot in a five-item list."""
        texts = ["这个可以吗", "这个可以的", "这个可以了"]
        assert extract_keywords(texts) == []

    def test_measure_word_debris_is_rejected(self) -> None:
        """'10个群' must not yield the term '个群'."""
        assert "个群" not in extract_keywords(["10个群的配置", "10个群都要", "10个群"])

    def test_ascii_terms_are_lowercased_and_kept(self) -> None:
        kws = extract_keywords(["检查 Nginx 配置", "重启 nginx", "nginx 报错"])
        assert "nginx" in kws

    def test_code_blocks_are_stripped(self) -> None:
        texts = ["```\nsecretvalue secretvalue\n```部署", "部署"]
        assert "secretvalue" not in extract_keywords(texts)

    def test_urls_do_not_become_keywords(self) -> None:
        texts = ["https://example.com/aaa 部署", "https://example.com/aaa 部署"]
        assert not any("example" in k for k in extract_keywords(texts))

    def test_longer_term_wins_over_its_prefix(self) -> None:
        texts = ["熔断器生效了", "熔断器没生效", "熔断器状态"]
        kws = extract_keywords(texts)
        assert "熔断器" in kws
        assert "熔断" not in kws

    def test_limit_is_respected(self) -> None:
        texts = ["部署 测试 分支 配置 服务 网关 缓存"] * 3
        assert len(extract_keywords(texts, limit=3)) == 3


class TestAnalyze:
    def test_counts_separate_conversation_from_tools(self) -> None:
        entries = [
            _user("部署服务"),
            _assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
            _user([{"type": "tool_result", "content": "done"}]),
            _assistant([{"type": "text", "text": "部署完成"}]),
        ]
        stats = analyze(entries)
        assert stats.prompts == 1
        assert stats.replies == 1
        assert stats.tools == (("Bash", 1),)

    def test_tool_errors_counted_for_bool_and_string_flags(self) -> None:
        """Transcripts carry is_error as a bool *or* the string 'True'."""
        entries = [
            _user([{"type": "tool_result", "is_error": True}]),
            _user([{"type": "tool_result", "is_error": "True"}]),
            _user([{"type": "tool_result", "is_error": False}]),
            _user([{"type": "tool_result"}]),
        ]
        assert analyze(entries).errors == 2

    def test_empty_window(self) -> None:
        stats = analyze([])
        assert stats == DigestStats()
        assert stats.is_empty is True

    def test_is_empty_false_with_activity(self) -> None:
        assert analyze([_user("hi")]).is_empty is False

    def test_tools_sorted_by_frequency(self) -> None:
        entries = [
            _assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
            _assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
            _assistant([{"type": "tool_use", "name": "Edit", "input": {}}]),
        ]
        assert analyze(entries).tools[0] == ("Bash", 2)

    def test_malformed_entries_do_not_raise(self) -> None:
        analyze([{}, {"type": "user"}, {"type": "user", "message": None}])


def _at(entry: dict, seconds: int) -> dict:
    """Stamp an entry at a fixed offset from a known epoch."""
    base = dt.datetime(2026, 7, 28, 0, 0, 0, tzinfo=dt.UTC)
    return {**entry, "timestamp": (base + dt.timedelta(seconds=seconds)).isoformat()}


class TestRounds:
    def test_a_round_spans_prompt_to_next_prompt(self) -> None:
        entries = [
            _at(_user("第一个问题"), 0),
            _at(_assistant([{"type": "text", "text": "答"}]), 60),
            _at(_user("第二个问题"), 120),
            _at(_assistant([{"type": "text", "text": "答"}]), 150),
        ]
        rounds = analyze(entries).rounds
        assert [r.prompt for r in rounds] == ["第一个问题", "第二个问题"]

    def test_active_time_measures_work_not_absence(self) -> None:
        """A prompt answered in a minute is not a two-hour conversation."""
        entries = [
            _at(_user("问题"), 0),
            _at(_assistant([{"type": "text", "text": "答"}]), 60),
            _at(_user("下一个"), 7200),  # user came back two hours later
        ]
        assert analyze(entries).rounds[0].active_seconds == 60

    def test_idle_gap_inside_a_round_is_clamped(self) -> None:
        """Wall clock would call this an hour of work; it was five minutes."""
        entries = [
            _at(_user("问题"), 0),
            _at(_assistant([{"type": "text", "text": "答"}]), 3600),
        ]
        assert analyze(entries).rounds[0].active_seconds == 300

    def test_turns_count_assistant_activity(self) -> None:
        entries = [
            _at(_user("问题"), 0),
            _at(_assistant([{"type": "tool_use", "name": "Bash", "input": {}}]), 10),
            _at(_assistant([{"type": "text", "text": "好了"}]), 20),
        ]
        assert analyze(entries).rounds[0].turns == 2

    def test_tool_results_do_not_split_a_round(self) -> None:
        """Tool traffic is part of the exchange, not a new question."""
        entries = [
            _at(_user("问题"), 0),
            _at(_assistant([{"type": "tool_use", "name": "Bash", "input": {}}]), 10),
            _at(_user([{"type": "tool_result", "content": "ok"}]), 20),
            _at(_assistant([{"type": "text", "text": "好了"}]), 30),
        ]
        assert len(analyze(entries).rounds) == 1

    def test_busiest_ranks_by_active_time(self) -> None:
        entries = [
            _at(_user("短的"), 0),
            _at(_assistant([{"type": "text", "text": "a"}]), 30),
            _at(_user("长的"), 60),
            _at(_assistant([{"type": "text", "text": "b"}]), 300),
        ]
        busiest = analyze(entries).busiest_rounds(1)
        assert busiest[0].prompt == "长的"

    def test_busiest_limit_is_respected(self) -> None:
        entries = []
        for i in range(5):
            entries.append(_at(_user(f"问题{i}"), i * 100))
            entries.append(
                _at(_assistant([{"type": "text", "text": "答"}]), i * 100 + 10)
            )
        assert len(analyze(entries).busiest_rounds(3)) == 3

    def test_entries_without_timestamps_do_not_raise(self) -> None:
        stats = analyze([_user("问题"), _assistant([{"type": "text", "text": "答"}])])
        assert stats.rounds[0].active_seconds == 0
        assert stats.rounds[0].turns == 1

    def test_no_prompt_means_no_rounds(self) -> None:
        assert analyze([_assistant([{"type": "text", "text": "答"}])]).rounds == ()


class TestEntryTs:
    def test_parses_z_suffix(self) -> None:
        assert entry_ts({"timestamp": "2026-07-28T00:00:00Z"}) is not None

    def test_missing_or_malformed(self) -> None:
        assert entry_ts({}) is None
        assert entry_ts({"timestamp": "not a date"}) is None
        assert entry_ts({"timestamp": 12345}) is None
