"""A long transformation must survive an interruption.

A transformation of a long document reads it in parts, and each part is a
generation call. Measured on the hardware this was written on: a 46,000-token
book is ten parts, and one transformation is over an hour. The interruption
that mattered was the device restarting - on a phone, a matter of the battery
- and it threw away every part that had been analysed, so the next attempt
started again at the first part, and if the device restarted again it happened
again.

So the parts are kept as they are produced, and a restarted transformation
picks up where it left off. These tests check that, and the two things that
must not go wrong with it: a part must not be counted twice, and a document
whose text has changed must not resume onto the analysis of the text it used
to have.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from open_notebook.domain.notebook import Source
from open_notebook.utils import partials
from open_notebook.utils.long_document import split_for_context

END_MARKER = "THE-VERY-LAST-SENTENCE-OF-THE-DOCUMENT"


class _FakeChain:
    def __init__(self, num_ctx: int | None, answer: str = "an answer"):
        self.num_ctx = num_ctx
        self.answer = answer
        self.max_tokens: int | None = None
        self.requests: list[list] = []
        self.reasoning: bool | str | None = None

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        return AIMessage(content=self.answer)

    @property
    def map_requests(self) -> list[list]:
        return [
            messages
            for messages in self.requests
            if str(messages[-1].content).strip() != ""
        ]


def _document(paragraphs: int = 120) -> str:
    body = " ".join(["word"] * 100) + "."
    text = "\n\n".join(f"Paragraph {i}. {body}" for i in range(paragraphs))
    return f"{text}\n\n{END_MARKER}"


def _source(text: str):
    """A real Source, because the transformation checks the type.

    A mock would be turned into `None` and the parts would have nowhere to be
    kept - which is the thing the tests are about.
    """
    from open_notebook.domain.notebook import Source

    source = Source(id="source:test123", title="Test Source", asset=None)
    source.full_text = text
    return source


def _patched_add_insight():
    """The insight is written to the database, which the tests do not have."""
    return AsyncMock(return_value="command:ok")


def _transformation():
    transformation = MagicMock(title="Dense Summary", prompt="Summarise this.")
    transformation.id = "transformation:abc123"
    return transformation


async def _run(
    text: str,
    num_ctx: int = 4096,
    answer: str = "an answer",
    load=None,
    store=None,
    clear=None,
):
    """Run the transformation with the database and the model both stubbed.

    The partials live in the database, and the model is a real service; the
    tests are about the wiring between them, so both are replaced.
    """
    chains: list[_FakeChain] = []

    async def _provision(content, model_id, default_type, **kwargs):
        chain = _FakeChain(num_ctx, answer)
        chain.max_tokens = kwargs.get("max_tokens")
        chains.append(chain)
        return chain

    async def _default_load(source_id, transformation_id, content, split_key):
        return {}

    async def _default_store(
        source_id, transformation_id, content, split_key, index, partial
    ):
        return None

    async def _default_clear(source_id, transformation_id):
        return None

    with (
        patch(
            "open_notebook.graphs.transformation.provision_langchain_model",
            new=_provision,
        ),
        patch.object(Source, "add_insight", new=_patched_add_insight()) as add_insight,
        patch.object(partials, "load", new=load or _default_load),
        patch.object(partials, "store", new=store or _default_store),
        patch.object(partials, "clear", new=clear or _default_clear),
    ):
        from open_notebook.graphs.transformation import run_transformation

        state = {
            "source": _source(text),
            "input_text": text,
            "transformation": _transformation(),
        }
        result = await run_transformation(state, config={"configurable": {}})
    return state, result, chains, add_insight


class TestThePartialsAreKept:
    @pytest.mark.asyncio
    async def test_each_part_is_kept_as_it_is_produced(self):
        """Kept one at a time, not at the end.

        A transformation that is interrupted half way through has to have kept
        the half it finished - keeping them all at the end would keep none of
        them when the interruption comes.
        """
        text = _document()
        stored: list[int] = []

        async def _store(
            source_id, transformation_id, content, split_key, index, partial
        ):
            stored.append(index)

        await _run(text, store=_store)

        expected = len(split_for_context(text, budget_tokens=2644))
        assert stored == list(range(expected)), (
            f"the parts were kept as {stored}, not one at a time in order"
        )

    @pytest.mark.asyncio
    async def test_the_parts_are_cleared_once_the_answer_is_written(self):
        """Otherwise the next transformation of the same source would resume
        onto them, and answer with the old document's analysis."""
        cleared: list[str] = []

        async def _clear(source_id, transformation_id):
            cleared.append(transformation_id)

        state, _result, _chains, add_insight = await _run(_document(), clear=_clear)

        assert cleared == ["transformation:abc123"]
        add_insight.assert_awaited_once()


class TestTheTransformationResumes:
    @pytest.mark.asyncio
    async def test_a_restarted_transformation_skips_the_parts_already_done(self):
        """The point of keeping them.

        The first run is interrupted after two parts. The second run must not
        analyse those two parts again - they cost minutes each, and they are
        already done.
        """
        text = _document()
        expected = len(split_for_context(text, budget_tokens=2644))
        assert expected > 2, "the document should need more than two parts"

        done = {0: "the analysis of part one", 1: "the analysis of part two"}

        async def _load(source_id, transformation_id, content, split_key):
            return dict(done)

        _state, _result, chains, _add = await _run(text, load=_load)

        analysed = sum(len(chain.map_requests) for chain in chains)
        assert analysed == expected - len(done), (
            f"{analysed} parts were analysed, but {len(done)} of the {expected} "
            f"had already been done"
        )

        # And the analyses that were already done are in the answer's input,
        # so the answer is still made of every part.
        combined = "\n".join(
            str(messages[0].content)
            for chain in chains
            for messages in chain.requests
            if str(messages[-1].content).strip() == ""
        )
        assert "the analysis of part one" in combined
        assert "the analysis of part two" in combined

    @pytest.mark.asyncio
    async def test_a_changed_document_does_not_resume(self):
        """The analyses are of the text that was there.

        A source whose extraction has been redone needs its own analysis, and
        resuming onto the old one would answer a question about a document
        that is no longer the document.
        """
        text = _document()

        async def _load(source_id, transformation_id, content, split_key):
            # The loader itself decides this: it compares the hash. Here the
            # wiring is what matters - the transformation asks the loader
            # with the text it is working on.
            assert content == text
            return {0: "an analysis of something else"}

        await _run(text, load=_load)

        # The module's own comparison is what makes this safe.
        assert partials.content_hash(text) != partials.content_hash(text + " more")


class TestThePartialsModule:
    def test_the_record_id_is_the_same_for_the_same_pair(self):
        assert partials._record_id("source:a", "transformation:b") == (
            partials._record_id("source:a", "transformation:b")
        )

    def test_the_record_id_differs_for_a_different_pair(self):
        assert partials._record_id("source:a", "transformation:b") != (
            partials._record_id("source:a", "transformation:c")
        )

    def test_the_content_hash_changes_with_the_text(self):
        assert partials.content_hash("one") != partials.content_hash("two")
        assert partials.content_hash("one") == partials.content_hash("one")

    @pytest.mark.asyncio
    async def test_a_different_split_is_not_resumed(self):
        """The parts are the document cut up.

        The first part of a document cut into ten is not the first part of the
        same document cut into eighteen. Resuming across that would analyse
        one piece of the document and call it another - and the answer would
        be made of parts of two different documents, cut differently.
        """
        stored: dict[str, Any] = {
            "partials": {"0": "an analysis of the first tenth of the document"},
            "content_hash": partials.content_hash("the document"),
            "split_key": "10:6000:8192",
        }

        async def _query(query, vars=None):
            assert "split_key" in query, "the split has to be checked"
            return [stored]

        with patch.object(partials, "repo_query", new=_query):
            same = await partials.load(
                "source:a", "transformation:b", "the document", "10:6000:8192"
            )
            assert same == {0: stored["partials"]["0"]}

            different = await partials.load(
                "source:a", "transformation:b", "the document", "18:3000:8192"
            )
            assert different == {}, "a different cut is a different set of parts"

    @pytest.mark.asyncio
    async def test_changed_text_is_not_resumed(self):
        """The analyses are of the text that was there."""
        stored: dict[str, Any] = {
            "partials": {"0": "an analysis of the old text"},
            "content_hash": partials.content_hash("the old text"),
            "split_key": "10:6000:8192",
        }

        async def _query(query, vars=None):
            return [stored]

        with patch.object(partials, "repo_query", new=_query):
            resumed = await partials.load(
                "source:a", "transformation:b", "a new text", "10:6000:8192"
            )
            assert resumed == {}
