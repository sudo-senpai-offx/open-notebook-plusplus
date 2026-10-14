"""Retrieval-backed chat context.

Why these tests exist
--------------------
The feature they cover was written against a guessed response shape and
silently retrieved nothing: ``fn::vector_search`` does not return
``content``, it returns ``matches`` - an array of the document's matching
passages, already grouped per document with ``math::max(similarity)``.
Reading ``row["content"]`` yields None for every row, so the function
returned empty context, the caller treated that as "the index is empty",
and the whole-document dump went out as if nothing had changed. Nothing
errored. Every symptom looked like an empty corpus.

So the first test here reads the real shape, and the rest pin the
behaviour that shape has to produce.
"""

import pytest

from open_notebook.utils.context_builder import (
    MAX_CONTEXT_CHARACTERS,
    RETRIEVAL_MINIMUM_SCORE,
    build_relevant_context,
)

# One row exactly as fn::vector_search returns it: grouped by document,
# chunks flattened into `matches`, similarity already maximised.
SOURCE_ROW = {
    "id": "source:abc",
    "parent_id": "source:abc",
    "title": "Prokaryotic Gene Regulation",
    "matches": ["transcription begins at a promoter", "the operon is a unit"],
    "similarity": 0.56,
}
NOTE_ROW = {
    "id": "note:def",
    "parent_id": "note:def",
    "title": "Day 1 notes",
    "matches": ["Grammar practice"],
    "similarity": 0.51,
}


@pytest.fixture
def fake_search(monkeypatch):
    """Replace vector_search so the tests need no database or model."""
    calls = []

    def install(rows, error=None):
        async def _search(**kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return list(rows)

        monkeypatch.setattr(
            "open_notebook.domain.notebook.vector_search", _search, raising=False
        )
        return calls

    return install


class TestTheResponseShape:
    def test_chunks_come_from_matches_not_content(self, fake_search):
        """The regression. `content` is not in the response at all."""
        calls = fake_search([SOURCE_ROW])

        context, _, report = __import__("asyncio").run(
            build_relevant_context(None, "gene regulation")
        )

        assert calls, "vector_search should have been called"
        assert context["sources"][0]["relevant_content"]
        assert (
            "transcription begins at a promoter"
            in (context["sources"][0]["relevant_content"])
        )
        assert report["documents"] == 1

    def test_a_row_with_no_matches_is_skipped(self, fake_search):
        fake_search([{**SOURCE_ROW, "matches": []}])

        context, _, report = __import__("asyncio").run(
            build_relevant_context(None, "q")
        )

        assert context["sources"] == []
        assert report["documents"] == 0

    def test_a_string_in_matches_is_tolerated(self, fake_search):
        """Defensive: one malformed row should not empty the prompt."""
        fake_search([{**SOURCE_ROW, "matches": "a bare string"}])

        context, _, _ = __import__("asyncio").run(build_relevant_context(None, "q"))

        assert context["sources"][0]["relevant_content"] == "a bare string"

    def test_notes_and_sources_are_separated(self, fake_search):
        fake_search([SOURCE_ROW, NOTE_ROW])

        context, _, _ = __import__("asyncio").run(build_relevant_context(None, "q"))

        assert [s["id"] for s in context["sources"]] == ["source:abc"]
        assert [n["id"] for n in context["notes"]] == ["note:def"]


class TestOrdering:
    def test_strongest_document_first(self, fake_search):
        weak = {
            **SOURCE_ROW,
            "id": "source:weak",
            "parent_id": "source:weak",
            "similarity": 0.3,
        }
        fake_search([weak, SOURCE_ROW])

        context, _, _ = __import__("asyncio").run(build_relevant_context(None, "q"))

        assert context["sources"][0]["id"] == "source:abc"

    def test_similarity_is_rounded_for_display(self, fake_search):
        fake_search([{**SOURCE_ROW, "similarity": 0.56123456}])

        context, _, _ = __import__("asyncio").run(build_relevant_context(None, "q"))

        assert context["sources"][0]["similarity"] == 0.5612


class TestScope:
    def test_an_unselected_document_is_dropped(self, fake_search):
        fake_search([SOURCE_ROW, NOTE_ROW])

        context, _, report = __import__("asyncio").run(
            build_relevant_context(
                None, "q", eligible_ids={"sources": set(), "notes": {"note:def"}}
            )
        )

        assert context["sources"] == []
        assert report["dropped_out_of_scope"] == 1

    def test_no_config_means_everything_is_eligible(self, fake_search):
        fake_search([SOURCE_ROW])

        context, _, _ = __import__("asyncio").run(
            build_relevant_context(None, "q", eligible_ids=None)
        )

        assert context["sources"]


class TestBudget:
    def test_context_is_capped(self, fake_search):
        """Without a ceiling, eight generous sources overflow a 4k window."""
        huge = [
            {
                **SOURCE_ROW,
                "id": f"source:{i}",
                "parent_id": f"source:{i}",
                "matches": ["x" * 4000],
                "similarity": 0.5 - i * 0.001,
            }
            for i in range(8)
        ]
        fake_search(huge)

        _, _, report = __import__("asyncio").run(build_relevant_context(None, "q"))

        assert report["characters"] <= MAX_CONTEXT_CHARACTERS
        assert report["dropped_over_budget"] > 0


class TestDegradation:
    def test_a_failing_index_raises_for_the_caller_to_catch(self, fake_search):
        """The caller owns the fallback; this layer must not hide the reason."""
        fake_search([], error=RuntimeError("no embedding model"))

        with pytest.raises(RuntimeError, match="no embedding model"):
            __import__("asyncio").run(build_relevant_context(None, "q"))

    def test_nothing_matching_reports_empty_rather_than_falling_back(self, fake_search):
        fake_search([])

        context, _, report = __import__("asyncio").run(
            build_relevant_context(None, "an unrelated question")
        )

        assert report["mode"] == "empty"
        assert context["sources"] == []

    def test_the_report_shape_is_the_same_everywhere(self, fake_search):
        """A report that changes shape by outcome is a KeyError for the caller."""
        keys = None
        for rows in ([], [SOURCE_ROW]):
            fake_search(rows)
            _, _, report = __import__("asyncio").run(build_relevant_context(None, "q"))
            if keys is None:
                keys = set(report)
            assert set(report) == keys, f"report shape differs for rows={bool(rows)}"


class TestConstants:
    def test_the_similarity_bar_is_above_the_noise_floor(self):
        """0.44 was measured for an unrelated query; 0.2 passed it through."""
        assert RETRIEVAL_MINIMUM_SCORE >= 0.4

    def test_the_character_budget_leaves_room_in_a_4k_window(self):
        # ~4 characters per token, plus the prompt, the question and the
        # answer all sharing the same window.
        assert MAX_CONTEXT_CHARACTERS <= 16000
