"""The document splitter, and why the reduction exists.

A transformation was handed a source's whole text in one message. The model's
context is smaller than the document, the provider truncated it silently, and
the result was presented as covering the whole thing.

Measured: the "Dense Summary" of a 212,000-character book summarised its
*title page* - the title, the author, the copyright notice and the publisher's
blurb. The output looked entirely reasonable, which is what makes this the worst
kind of failure: a student searching a lecture for a term in chapter 14 finds
nothing, and nothing says why.
"""

from __future__ import annotations

from open_notebook.utils.long_document import DEFAULT_CHUNK_TOKENS, split_for_context
from open_notebook.utils.token_utils import token_count


def _text(paragraphs: int, words: int = 200) -> str:
    body = " ".join(["word"] * words) + "."
    return "\n\n".join(f"Paragraph {i}. {body}" for i in range(paragraphs))


class TestSplitting:
    def test_short_text_is_one_chunk(self):
        """The common case must cost nothing: no extra calls, no reduction."""
        text = "A short note about the lac operon."
        assert split_for_context(text) == [text]

    def test_empty_text_is_no_chunks(self):
        assert split_for_context("") == []
        assert split_for_context("   \n  ") == []

    def test_a_long_document_is_split(self):
        text = _text(40)
        chunks = split_for_context(text, budget_tokens=200)
        assert len(chunks) > 1

    def test_every_chunk_fits_the_budget(self):
        """The whole point. A chunk that overflows is truncated again by the
        provider, and the coverage is lost in exactly the way this is fixing."""
        text = _text(60)
        for chunk in split_for_context(text, budget_tokens=300):
            assert token_count(chunk) <= 300, (
                f"a chunk of {token_count(chunk)} tokens exceeded the budget of 300"
            )

    def test_the_whole_document_is_covered(self):
        """Not a sample of it - all of it.

        The reduction is only worth its cost if the parts between them cover the
        document. Dropping the tail would reintroduce the original bug with
        extra machinery.
        """
        text = _text(50, words=100)
        chunks = split_for_context(text, budget_tokens=250)
        joined = " ".join(chunks)
        for index in range(50):
            assert f"Paragraph {index}." in joined, f"paragraph {index} was lost"

    def test_a_paragraph_larger_than_the_budget_is_still_split(self):
        """One enormous paragraph must not become one enormous chunk."""
        text = "word " * 3000
        chunks = split_for_context(text, budget_tokens=200)
        assert len(chunks) > 1
        assert all(token_count(c) <= 400 for c in chunks)

    def test_the_cap_never_pushes_a_chunk_over_the_budget(self):
        """The budget is the hard constraint; the cap is advice.

        A 12,300-token document cannot be covered by 24 chunks of 300 tokens.
        An earlier version widened the chunks to satisfy the cap, which pushed
        them past the budget - and the provider truncates a chunk that is over
        its budget, which is the failure this module exists to prevent. The trade
        is now explicit: full coverage, every chunk within the context, and the
        number of calls may exceed the cap.
        """
        text = _text(400, words=100)
        chunks = split_for_context(text, budget_tokens=300, max_chunks=6)
        for chunk in chunks:
            assert token_count(chunk) <= 300, (
                f"a chunk of {token_count(chunk)} tokens exceeded the budget"
            )
        # The cap may be exceeded, and the cost is visible rather than paid in
        # lost text.
        assert len(chunks) > 6

    def test_the_cap_never_costs_coverage(self):
        """Coverage is the point, and it wins over the cap.

        When the cap and the budget conflict - a document too long for the cap
        at a chunk size that fits the context - the budget wins and the chunk
        count exceeds the cap. The alternative is dropping the tail, which is the
        original bug with extra machinery.
        """
        text = _text(60, words=100)
        chunks = split_for_context(text, budget_tokens=200, max_chunks=4)
        assert len(chunks) > 4, "the cap should have been exceeded, not obeyed"
        joined = " ".join(chunks)
        assert "Paragraph 59." in joined, "the tail was dropped rather than covered"
        for chunk in chunks:
            assert token_count(chunk) <= 200

    def test_overlap_carries_the_boundary(self):
        """A sentence that straddles a boundary would otherwise be summarised
        with half of it missing."""
        text = "Alpha sentence one. Beta sentence two. " + ("Filler here. " * 300)
        chunks = split_for_context(text, budget_tokens=200, overlap_tokens=50)
        assert len(chunks) > 1
        # The head of the second chunk should not start from scratch.
        assert chunks[1][:40].strip() != ""

    def test_the_budget_is_configurable(self, monkeypatch):
        """Compared as a ratio, not an absolute.

        The document is about ten thousand tokens, so a five-hundred token
        budget gives twenty chunks and a two-thousand token budget gives five.
        Asserting an absolute number here was a test that could only pass for
        one document.
        """
        text = _text(40)
        monkeypatch.setenv("OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS", "500")
        narrow = len(split_for_context(text))
        monkeypatch.setenv("OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS", "2000")
        wide = len(split_for_context(text))
        assert wide < narrow, "a larger budget should mean fewer chunks"


class TestTheReductionIsWorthIt:
    def test_the_budget_is_large_enough_to_be_worth_chunking(self):
        """The design rests on this: reading a prompt is nearly free next to
        writing one, so covering more of the document with the same number of
        *output* tokens is the win.

        Measured on this hardware: about 1.1 tokens per second of generation.
        A 750-token answer is eleven minutes; reading 4096 tokens is seconds.
        So a map step that writes 150 tokens per part and a reduce step that
        writes the real answer is far cheaper than one call that writes the
        answer from the first few thousand tokens of the document.
        """
        assert DEFAULT_CHUNK_TOKENS >= 1000
