"""Which part of a conversation the model is allowed to see.

`execute_chat` used to send the entire checkpoint history every turn.
Measured against the phone's `llama-server -c 4096`, that leaves about
2,400 tokens for history once the system prompt and retrieved context
are paid for - so a long session stops working with no error, which is
the worst way for it to fail.

The two properties that matter:

  * an exchange is never split. A model shown an answer with its
    question cut off answers something nobody asked, which is worse than
    it simply not remembering.
  * what was dropped is reported, so the UI can say so rather than the
    conversation quietly losing its beginnings.
"""

from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from open_notebook.utils.history import (
    DEFAULT_CONTEXT_TOKENS,
    REPLY_RESERVE_TOKENS,
    parse_since,
    select_history,
    split_exchanges,
)


def _exchange(n: int, size: int = 40) -> list:
    """One question and its answer, with an optional timestamp."""
    stamp = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc) - timedelta(hours=n)
    question = HumanMessage(content=f"question {n} " + "q" * size)
    answer = AIMessage(content=f"answer {n} " + "a" * size)
    for message in (question, answer):
        message.response_metadata = {"created": stamp.isoformat()}
    return [question, answer]


def _conversation(count: int, size: int = 40) -> list:
    """Oldest first, the way a real transcript reads.

    `_exchange(n)` dates itself `n` hours ago, so the newest exchange is
    n=0. Building the list in reverse is what puts them in order - and
    getting it wrong silently makes the trimmer keep the *oldest*
    exchanges, which looks like the trimmer working.
    """
    messages = []
    for n in reversed(range(count)):
        messages.extend(_exchange(n, size))
    return messages


class TestGrouping:
    def test_questions_and_answers_pair_up(self):
        groups = split_exchanges(_conversation(3))

        assert len(groups) == 3
        assert all(len(g) == 2 for g in groups)

    def test_a_trailing_question_is_kept_alone(self):
        """A question the model has not answered yet is still a turn."""
        messages = _conversation(2)
        messages.append(HumanMessage(content="and then?"))

        groups = split_exchanges(messages)

        assert len(groups) == 3
        assert len(groups[-1]) == 1

    def test_an_empty_history_yields_nothing(self):
        assert split_exchanges([]) == []


class TestBudget:
    def test_a_short_conversation_is_sent_whole(self):
        result = select_history(_conversation(5), context_tokens=4096)

        assert result.dropped_exchanges == 0
        assert result.kept_exchanges == 5

    def test_a_long_conversation_is_trimmed_from_the_oldest_end(self):
        result = select_history(_conversation(400, size=200), context_tokens=4096)

        assert result.dropped_exchanges > 0
        assert result.kept_exchanges < 400
        # The newest survives; the oldest is what goes.
        assert result.messages[-1].content.startswith("answer 0")

    def test_the_newest_exchange_always_survives(self):
        """Even when it alone is larger than the budget.

        Dropping it would leave the model answering a question it cannot
        see, which is worse than a long prompt.
        """
        result = select_history(_conversation(50, size=4000), context_tokens=1024)

        assert result.messages, "never return an empty history"
        assert result.messages[-1].content.startswith("answer 0")

    def test_an_exchange_is_never_split(self):
        result = select_history(_conversation(60, size=300), context_tokens=2048)

        kept = result.messages
        for index in range(0, len(kept) - 1, 2):
            assert kept[index].type == "human", f"split at index {index}"
            assert kept[index + 1].type == "ai"

    def test_the_result_fits_the_budget(self):
        result = select_history(_conversation(200, size=300), context_tokens=4096)

        assert result.used_tokens <= result.budget_tokens

    def test_the_budget_reserves_room_for_the_reply(self):
        result = select_history(_conversation(2), context_tokens=4096)

        assert result.budget_tokens == DEFAULT_CONTEXT_TOKENS - REPLY_RESERVE_TOKENS

    def test_the_report_says_what_was_dropped(self):
        result = select_history(_conversation(400, size=200), context_tokens=4096)
        summary = result.summary()

        assert summary["dropped_exchanges"] > 0
        assert summary["budget_tokens"] > 0
        assert summary["reason"] == "budget"


class TestTimeframe:
    def test_only_recent_exchanges_survive(self):
        result = select_history(
            _conversation(6),
            context_tokens=4096,
            history_since=datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc),
        )

        assert result.reason == "timeframe"
        assert result.dropped_exchanges > 0
        assert result.kept_exchanges >= 1

    def test_a_timeframe_still_respects_the_budget(self):
        """A long afternoon must not blow the context either."""
        result = select_history(
            _conversation(200, size=200),
            context_tokens=2048,
            history_since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )

        assert result.used_tokens <= result.budget_tokens

    def test_an_unparseable_timeframe_is_ignored_not_fatal(self):
        result = select_history(
            _conversation(3), context_tokens=4096, history_since="last tuesday-ish"
        )

        assert result.reason == "budget"
        assert result.dropped_exchanges == 0

    def test_undated_messages_survive_a_timeframe(self):
        """An old conversation has no timestamps in its checkpoint.

        Dropping them would make the timeframe control empty a session
        rather than narrow it.
        """
        undated = [HumanMessage(content="q"), AIMessage(content="a")]
        result = select_history(
            undated, context_tokens=4096, history_since=datetime.now(timezone.utc)
        )

        assert result.kept_exchanges == 1


class TestParseSince:
    @pytest.mark.parametrize(
        "text,delta",
        [
            ("45m", timedelta(minutes=45)),
            ("2h", timedelta(hours=2)),
            ("2d", timedelta(days=2)),
            ("1w", timedelta(weeks=1)),
        ],
    )
    def test_shorthand(self, text, delta):
        parsed = parse_since(text)
        expected = datetime.now(timezone.utc) - delta

        assert abs((parsed - expected).total_seconds()) < 5

    def test_an_iso_timestamp_is_used_as_given(self):
        parsed = parse_since("2026-10-14T12:00:00+00:00")

        assert parsed == datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def test_a_naive_timestamp_is_read_as_utc(self):
        """Two devices are not guaranteed to agree on a timezone."""
        assert parse_since("2026-10-14T12:00:00") == datetime(
            2026, 10, 14, 12, 0, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("value", [None, "", "not a time"])
    def test_unparseable_input_is_none(self, value):
        assert parse_since(value) is None


class TestConfiguration:
    def test_the_context_is_read_from_the_environment(self, monkeypatch):
        from api.routers.chat import _context_tokens

        monkeypatch.setenv("OPEN_NOTEBOOK_CHAT_CONTEXT", "8192")
        assert _context_tokens() == 8192

    def test_an_absurd_window_falls_back_rather_than_trusting_it(self, monkeypatch):
        """Assuming a big window is what makes a phone-sized one fail."""
        from api.routers.chat import _context_tokens

        monkeypatch.setenv("OPEN_NOTEBOOK_CHAT_CONTEXT", "16")
        assert _context_tokens() == DEFAULT_CONTEXT_TOKENS

    def test_nonsense_falls_back(self, monkeypatch):
        from api.routers.chat import _context_tokens

        monkeypatch.setenv("OPEN_NOTEBOOK_CHAT_CONTEXT", "plenty")
        assert _context_tokens() == DEFAULT_CONTEXT_TOKENS

    def test_the_launcher_keeps_the_window_in_one_place(self):
        """Two literals drift, and the symptom is a silent truncation."""
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[1] / "scripts/termux-launch.sh"
        ).read_text()

        assert 'LLAMA_CTX="${OPEN_NOTEBOOK_CHAT_CONTEXT:-4096}"' in script
        assert "-c $LLAMA_CTX" in script
        assert "OPEN_NOTEBOOK_CHAT_CONTEXT" in script
        assert "-c 4096 -np" not in script, "the window must not be a second literal"
