"""Instruction prefixes for embedding models that need one.

The load-bearing tests here are the two "must not prefix" ones. Applying an
E5 prefix to a BGE model is not a no-op - it moves the input off the
distribution the model was trained on, so retrieval degrades while still
*looking* like it works. That failure is silent, which is exactly why it
needs a test rather than a code review.
"""

from __future__ import annotations

import pytest

from open_notebook.utils.embedding_prefix import (
    EmbeddingRole,
    apply_prefix,
    prefix_for,
    requires_prefix,
)


class TestE5GetsItsInstruction:
    @pytest.mark.parametrize(
        "model",
        [
            "multilingual-e5-small",
            "/data/data/com.termux/files/home/gguf/multilingual-e5-small-q8_0.gguf",
            "intfloat/e5-base-v2",
            "intfloat/multilingual-e5-large",
            "E5-Small",  # case must not matter
        ],
    )
    def test_recognised(self, model: str):
        assert requires_prefix(model)

    def test_query_and_passage_differ(self):
        """Embedding a query as if it were a passage is the failure mode."""
        assert prefix_for("multilingual-e5-small", EmbeddingRole.QUERY) == "query: "
        assert prefix_for("multilingual-e5-small", EmbeddingRole.PASSAGE) == "passage: "

    def test_applied(self):
        assert (
            apply_prefix(
                "machine learning", "multilingual-e5-small", EmbeddingRole.QUERY
            )
            == "query: machine learning"
        )
        assert (
            apply_prefix("a document", "multilingual-e5-small", EmbeddingRole.PASSAGE)
            == "passage: a document"
        )


class TestNomicGetsItsOwnInstruction:
    def test_nomic_uses_search_prefixes(self):
        """Nomic's wording is not E5's; reusing E5's would be a wrong prefix,
        which is worse than none."""
        assert prefix_for("nomic-embed-text", EmbeddingRole.QUERY) == "search_query: "
        assert (
            prefix_for("nomic-embed-text", EmbeddingRole.PASSAGE) == "search_document: "
        )


class TestBgeMustStayBare:
    """The regression that matters.

    The PC runs `bge-m3` and was retrieving correctly. A global "add the E5
    prefix" change would have degraded it, invisibly.
    """

    @pytest.mark.parametrize(
        "model",
        [
            "bge-m3",
            "/data/data/com.termux/files/home/gguf/bge-m3-Q4_K_M.gguf",
            "bge-small-en-v1.5",
            "BGE-large-en",
        ],
    )
    def test_bge_is_never_prefixed(self, model: str):
        assert not requires_prefix(model)
        assert prefix_for(model, EmbeddingRole.QUERY) == ""
        assert prefix_for(model, EmbeddingRole.PASSAGE) == ""
        assert apply_prefix("text", model, EmbeddingRole.QUERY) == "text"

    def test_a_bge_path_is_not_matched_by_the_e5_rule(self):
        """Names are matched by substring, so check the two do not collide."""
        assert requires_prefix("multilingual-e5-small")
        assert not requires_prefix("bge-m3")


class TestUnknownModelsAreLeftAlone:
    @pytest.mark.parametrize("model", ["", "unknown", "some-custom-model", None])
    def test_no_prefix_without_evidence(self, model):
        """Conservative by default: a wrong prefix is worse than none, and
        every model behaved this way before prefixes existed."""
        assert prefix_for(model, EmbeddingRole.QUERY) == ""
        assert apply_prefix("text", model, EmbeddingRole.PASSAGE) == "text"


class TestIdempotence:
    def test_prefixing_twice_does_not_double_it(self):
        """Chunking calls this more than once for the same string, and
        `"query: query: x"` is a different input from what the model saw in
        training."""
        once = apply_prefix("hello", "multilingual-e5-small", EmbeddingRole.QUERY)
        twice = apply_prefix(once, "multilingual-e5-small", EmbeddingRole.QUERY)

        assert once == twice == "query: hello"

    def test_empty_text_stays_empty(self):
        assert apply_prefix("", "multilingual-e5-small", EmbeddingRole.QUERY) == ""

    def test_a_different_role_does_not_see_the_other_prefix_as_present(self):
        """`query: x` must still be able to receive a passage prefix; the
        idempotence check is per-prefix, not per-text."""
        result = apply_prefix(
            "query: hello", "multilingual-e5-small", EmbeddingRole.PASSAGE
        )

        assert result == "passage: query: hello"


class TestRoleIsExplicit:
    def test_roles_are_distinct(self):
        assert EmbeddingRole.QUERY is not EmbeddingRole.PASSAGE

    def test_role_is_a_string_enum(self):
        """Passed through logs and JSON; a plain str keeps that cheap."""
        assert EmbeddingRole.QUERY.value == "query"
        assert EmbeddingRole.PASSAGE.value == "passage"
