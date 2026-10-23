"""The transformation must never hand the model more than its window can hold.

Two failures live here, and both of them were silent.

The first is duplication. The instructions for a part of a long document used
to carry the part, and the part was also sent as the user message - so a part
of seven thousand tokens arrived as fourteen thousand, the provider truncated
it, and the model analysed the first few thousand tokens of every part. The
split was correct and the coverage was not, which is worse than not splitting
at all, because the result was presented as covering the whole document.

The second is a budget the model cannot honour. The chunk size was
configuration, the context window was the credential's, and nothing checked
that one fitted inside the other. A part larger than the window is truncated
exactly as silently as the one above.

So the tests below check the requests that are actually made, rather than the
arithmetic that is supposed to produce them. A test that only checks the
arithmetic would have passed while the duplication was there, because the
arithmetic was never the thing that was broken.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from loguru import logger

from open_notebook.utils.long_document import (
    budget_message,
    budget_within_context,
    chunk_budget,
)
from open_notebook.utils.token_utils import token_count

#: A marker at the very end of the document. It cannot be carried into another
#: chunk by the overlap, because the overlap repeats the *tail* of the previous
#: chunk and this is the end of the last one - so wherever it appears, it
#: appears once per copy of the chunk that holds it.
END_MARKER = "THE-VERY-LAST-SENTENCE-OF-THE-DOCUMENT"


class _FakeChain:
    """A model that remembers what it was asked."""

    def __init__(self, num_ctx: int | None, answer: str = "an answer"):
        self.num_ctx = num_ctx
        self.answer = answer
        self.max_tokens: int | None = None
        self.requests: list[list] = []
        #: Set by the transformation, and the point of the map step's chain.
        self.reasoning: bool | str | None = None

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        response = AIMessage(content=self.answer)
        return response

    @property
    def request_tokens(self) -> list[int]:
        return [
            sum(token_count(str(m.content)) for m in messages)
            for messages in self.requests
        ]


def _fake_provisioner(num_ctx: int | None, answer: str = "an answer"):
    """A stand-in for `provision_langchain_model` that records each chain."""
    made: list[_FakeChain] = []

    async def _provision(content, model_id, default_type, **kwargs):
        chain = _FakeChain(num_ctx, answer)
        chain.max_tokens = kwargs.get("max_tokens")
        made.append(chain)
        return chain

    return _provision, made


def _document(paragraphs: int = 60, words: int = 120) -> str:
    body = " ".join(["word"] * words) + "."
    text = "\n\n".join(f"Paragraph {i}. {body}" for i in range(paragraphs))
    return f"{text}\n\n{END_MARKER}"


def _state(text: str):
    transformation = MagicMock(title="Dense Summary", prompt="Summarise this.")
    return {
        "source": None,
        "input_text": text,
        "transformation": transformation,
    }


async def _run(text: str, num_ctx: int | None, answer: str = "an answer"):
    provision, made = _fake_provisioner(num_ctx, answer)
    with patch(
        "open_notebook.graphs.transformation.provision_langchain_model",
        new=provision,
    ):
        from open_notebook.graphs.transformation import run_transformation

        await run_transformation(_state(text), config={"configurable": {}})
    return made


def _is_combine(messages) -> bool:
    """A combine request has nothing in the user turn - the partials are in
    the instructions - while a map request carries the part."""
    return str(messages[-1].content).strip() == ""


class TestTheWindowIsRespected:
    @pytest.mark.asyncio
    async def test_no_request_exceeds_the_models_window(self):
        """The invariant, stated on the requests themselves.

        A window of 4096 and a document long enough to need splitting: every
        request the model is asked to answer has to fit inside 4096 tokens,
        instructions included. A part that did not fit would be truncated by
        the provider, silently, and the answer would be presented as covering
        the whole document.
        """
        num_ctx = 4096
        made = await _run(_document(), num_ctx)

        requests = [tokens for chain in made for tokens in chain.request_tokens]
        assert requests, "the model was never asked anything"
        for tokens in requests:
            assert tokens <= num_ctx, (
                f"a request of {tokens} tokens was sent to a model whose window "
                f"is {num_ctx}"
            )

    @pytest.mark.asyncio
    async def test_the_part_is_not_sent_twice(self):
        """The duplication, caught by counting.

        The marker sits at the end of the document, so it belongs to exactly one
        part. It must appear exactly once in that part's request - once as the
        user message and not again inside the instructions.
        """
        made = await _run(_document(), 4096)

        holders = [
            chain
            for chain in made
            for messages in chain.requests
            if any(END_MARKER in str(m.content) for m in messages)
        ]
        assert holders, "the end of the document was never sent to the model"

        checked = 0
        for chain in holders:
            for messages in chain.requests:
                text = "\n".join(str(m.content) for m in messages)
                if END_MARKER not in text:
                    continue
                checked += 1
                assert text.count(END_MARKER) == 1, (
                    "the part appears more than once in a single request, so the "
                    "provider will truncate it and the model will analyse only "
                    "the start of it"
                )
        assert checked, "no request carried the end of the document"

    @pytest.mark.asyncio
    async def test_the_whole_document_reaches_the_model(self):
        """Every part is sent, not just the first few.

        The end of the document has to be in a request somewhere. If the
        splitter dropped the tail, or the map step only ran on the first part,
        this would fail - and the insight would still look reasonable.
        """
        made = await _run(_document(), 4096)

        sent = "\n".join(
            str(m.content)
            for chain in made
            for messages in chain.requests
            for m in messages
        )
        assert END_MARKER in sent, "the end of the document was never sent"

    @pytest.mark.asyncio
    async def test_a_document_that_fits_is_read_in_one_pass(self):
        """The common case must still cost one call.

        A short document needs no splitting and no reduction - it is simply
        read. The guard on the window must not turn a document that fits into
        several.
        """
        made = await _run("A short note about the lac operon.", 4096)

        calls = [messages for chain in made for messages in chain.requests]
        assert len(calls) == 1, "a document that fits should need one call"

    @pytest.mark.asyncio
    async def test_the_answer_is_bounded_by_the_room_left(self):
        """The model must be told how much room it has.

        Asking for more than the window can give does not give more: the
        provider stops the generation at the window and the answer is cut off
        mid-sentence, which the caller cannot tell from a complete answer.
        """
        made = await _run(_document(), 4096)

        spoken = [chain for chain in made if chain.requests]
        assert spoken, "the model was never asked anything"
        for chain in spoken:
            assert chain.max_tokens is not None
            assert chain.max_tokens <= 4096, (
                f"a chain was provisioned for {chain.max_tokens} tokens of "
                f"output in a window of 4096, which leaves no room for the "
                f"document"
            )


class TestTheReductionFitsTheWindow:
    """The partial analyses of a long document can add up to more than the
    model's window, and a combine prompt that does not fit is truncated by the
    provider - which would drop the last parts and leave an answer that claims
    to cover the document while covering most of it."""

    @pytest.mark.asyncio
    async def test_partials_that_do_not_fit_are_combined_in_batches(self):
        """Each part writes a long analysis, so the partials add up.

        The partials of a long document can add up to more than the model's
        window, and a combine prompt that does not fit is truncated by the
        provider - which would drop the last parts and leave an answer that
        claims to cover the document while covering most of it. So they are
        combined in batches that do fit, and the results of those are combined
        again.
        """
        logged: list[str] = []
        sink = logger.add(logged.append, level="INFO")
        try:
            made = await _run(_document(paragraphs=600), 4096, answer="word " * 300)
        finally:
            logger.remove(sink)

        reductions = [m for m in logged if "partial analyses to" in m]
        assert reductions, (
            "the partial analyses did not fit the window, so they should have "
            f"been reduced in batches; the log said: {logged}"
        )

        for messages in [
            messages
            for chain in made
            for messages in chain.requests
            if _is_combine(messages)
        ]:
            tokens = sum(token_count(str(m.content)) for m in messages)
            assert tokens <= 4096, (
                f"a combine prompt of {tokens} tokens was sent to a model whose "
                f"window is 4096"
            )

    @pytest.mark.asyncio
    async def test_a_window_too_small_to_reduce_reports_the_loss(self):
        """When even one partial does not fit, there is nothing smaller to
        reduce to - and the loss is reported rather than hidden.

        A window of 1024 cannot hold a part, the instructions and an answer
        together. The transformation cannot fix that; it can only say so.
        """
        logged: list[str] = []
        sink = logger.add(logged.append, level="WARNING")
        try:
            await _run(_document(paragraphs=60), 1024, answer="word " * 300)
        finally:
            logger.remove(sink)

        assert any("cannot be reduced further" in message for message in logged), (
            f"the loss should have been reported; the log said: {logged}"
        )

    @pytest.mark.asyncio
    async def test_the_partials_are_the_text_and_not_the_message(self):
        """The partial analyses go into the combine prompt verbatim.

        They used to be the message's *repr* - "content='...' additional_
        kwargs={} response_metadata={}" - because the extractor was handed
        the message instead of its content. The combine step then spent its
        input budget on that wrapper, and read the text with its newlines
        escaped.
        """
        made = await _run(_document(), 4096, answer="The listening habit matters.")

        combines = [
            messages
            for chain in made
            for messages in chain.requests
            if _is_combine(messages)
        ]
        assert combines, "the parts were never combined"
        prompt = str(combines[-1][0].content)
        assert "The listening habit matters." in prompt
        for noise in ("additional_kwargs", "response_metadata", "tool_calls"):
            assert noise not in prompt, (
                f"the combine prompt contains {noise!r}, so the partial was "
                f"the message's repr rather than its text"
            )

    @pytest.mark.asyncio
    async def test_partials_that_fit_are_combined_once(self):
        """The ordinary case: one combine, no batching."""
        made = await _run(_document(), 4096)

        combines = [
            messages
            for chain in made
            for messages in chain.requests
            if _is_combine(messages)
        ]
        assert len(combines) == 1, "the partials fitted, so one combine is enough"


class TestTheMapStepDoesNotThink:
    """A thinking model spends the map step's whole budget thinking.

    Measured on qwen3:4b: five hundred tokens of reasoning and no analysis at
    all, so the part came back empty. Ten parts, ten holes, and an answer
    that covered the document in the same way it always had - by not covering
    it.
    """

    @pytest.mark.asyncio
    async def test_the_map_step_turns_the_reasoning_off(self):
        made = await _run(_document(), 4096)

        map_chains = [
            chain
            for chain in made
            if any(not _is_combine(messages) for messages in chain.requests)
        ]
        assert map_chains, "the map step never ran"
        for chain in map_chains:
            assert chain.reasoning is False, (
                "the map step should not be reasoning: at its output budget the "
                "reasoning is all the model has room for"
            )

    @pytest.mark.asyncio
    async def test_the_final_answer_is_left_to_reason(self):
        """Only the map step is mechanical. The reduce step is a synthesis, and
        the budget for it is not small."""
        made = await _run(_document(), 4096)

        combine_chains = [
            chain
            for chain in made
            if chain.requests and all(_is_combine(m) for m in chain.requests)
        ]
        assert combine_chains, "the parts were never combined"
        for chain in combine_chains:
            assert chain.reasoning is not False, (
                "the reduce step should keep the model's reasoning"
            )


class TestTheBudgetFitsTheWindow:
    def test_a_budget_larger_than_the_window_is_clamped(self):
        """The window wins over the configuration.

        A chunk larger than the window is truncated by the provider, which is
        the failure the whole splitting path exists to prevent. So when the
        configured budget does not fit, it is reduced rather than obeyed.
        """
        budget = budget_within_context(4096, reserved_tokens=1500)
        assert budget <= 4096 - 1500
        assert budget + 1500 <= 4096

    def test_a_budget_that_fits_is_left_alone(self):
        """The common case must not be made smaller for nothing."""
        with patch.dict(
            "os.environ", {"OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS": "1000"}
        ):
            assert budget_within_context(8192, reserved_tokens=500) == 1000

    def test_an_unknown_window_falls_back_to_the_configuration(self):
        """A provider that does not expose a window leaves nothing to check
        against, and the configured budget is then the best guess there is."""
        with patch.dict(
            "os.environ", {"OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS": "1234"}
        ):
            assert budget_within_context(None, reserved_tokens=500) == 1234
            assert chunk_budget() == 1234


class TestWhatTheBudgetReports:
    """budget_within_context says the difference between the wish and the fact
    is reported by the caller. These are the reports it has to produce."""

    def test_a_clamped_budget_says_what_it_was_clamped_to(self):
        """A device that configured a budget its model cannot honour must find
        out, rather than quietly losing text."""
        with patch.dict(
            "os.environ", {"OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS": "3000"}
        ):
            wanted = chunk_budget()
            budget = budget_within_context(4096, reserved_tokens=1500)

        message = budget_message(4096, budget, wanted, reserved_tokens=1500)

        assert message is not None
        assert str(wanted) in message, "the configured budget must be named"
        assert "4096" in message, "the window must be named"
        assert str(budget) in message, "the size actually used must be named"
        assert budget < wanted

    def test_an_unknown_window_is_reported_as_a_guess(self):
        """This is the case that arrives later as an opaque rejection: the
        parts are sized by assumption, and nothing else records that."""
        with patch.dict(
            "os.environ", {"OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS": "3000"}
        ):
            wanted = chunk_budget()
            budget = budget_within_context(None, reserved_tokens=701)

        message = budget_message(None, budget, wanted, reserved_tokens=701)

        assert message is not None
        assert "does not report a context window" in message
        # The number that decides whether the request survives: everything the
        # provider must hold at once, not just the part.
        assert str(wanted + 701) in message

    def test_a_budget_that_fits_says_nothing(self):
        """The common case must stay quiet, or the report stops meaning
        anything when there is one."""
        with patch.dict(
            "os.environ", {"OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS": "1000"}
        ):
            wanted = chunk_budget()
            budget = budget_within_context(8192, reserved_tokens=500)

        assert budget_message(8192, budget, wanted, reserved_tokens=500) is None
