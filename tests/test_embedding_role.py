"""The role reaches the provider, and the query path is the one that sets it.

`generate_embedding` serves both sides of a search - `vector_search` embeds a
query through it, and `embedding_commands` embeds stored content through it -
so the role cannot be inferred from the function being called. It has to be
passed, and these tests exist because a dropped argument is silent: the wrong
prefix still returns vectors, so nothing fails and retrieval just gets worse.
"""

from __future__ import annotations

import pytest

from open_notebook.utils import embedding as embmod
from open_notebook.utils.embedding_prefix import EmbeddingRole


class _FakeModel:
    """Records exactly what the provider was asked to embed."""

    def __init__(self):
        self.seen: list[list[str]] = []
        self.model_name = "multilingual-e5-small"

    async def aembed(self, texts):
        self.seen.append(list(texts))
        return [[0.1] * 8 for _ in texts]


@pytest.fixture
def fake(monkeypatch):
    model = _FakeModel()

    async def get_embedding_model(**kwargs):
        return model

    import open_notebook.ai.models as models_mod

    monkeypatch.setattr(
        models_mod.model_manager, "get_embedding_model", get_embedding_model
    )
    return model


class TestDocumentsArePassages:
    @pytest.mark.asyncio
    async def test_default_role_prefixes_as_a_passage(self, fake):
        await embmod.generate_embeddings(["stored content"])

        assert fake.seen == [["passage: stored content"]]

    @pytest.mark.asyncio
    async def test_chunked_documents_keep_the_passage_prefix(self, fake, monkeypatch):
        monkeypatch.setattr(embmod, "CHUNK_SIZE", 1)  # force chunking
        monkeypatch.setattr(embmod, "chunk_text", lambda *a, **k: ["one", "two"])

        await embmod.generate_embedding("a long document", role=EmbeddingRole.PASSAGE)

        assert fake.seen[-1] == ["passage: one", "passage: two"]


class TestQueriesAreQueries:
    @pytest.mark.asyncio
    async def test_query_role_prefixes_as_a_query(self, fake):
        await embmod.generate_embedding(
            "what is gradient descent", role=EmbeddingRole.QUERY
        )

        assert fake.seen == [["query: what is gradient descent"]]

    @pytest.mark.asyncio
    async def test_the_role_survives_delegation_to_the_batch_call(self, fake):
        """The single-vector function delegates for short text. A role dropped
        at that seam would embed queries as passages - the exact bug the
        parameter exists to prevent."""
        await embmod.generate_embedding("short", role=EmbeddingRole.QUERY)

        assert fake.seen[0][0].startswith("query: ")

    @pytest.mark.asyncio
    async def test_no_double_prefix_through_delegation(self, fake):
        await embmod.generate_embedding("short", role=EmbeddingRole.QUERY)

        assert fake.seen[0][0].count("query:") == 1


class TestBgeIsUnaffectedEndToEnd:
    @pytest.mark.asyncio
    async def test_a_bge_model_receives_bare_text(self, fake):
        """The PC runs bge-m3. If this ever regressed, the side that was
        already retrieving correctly would silently get worse."""
        fake.model_name = "bge-m3"

        await embmod.generate_embedding("machine learning", role=EmbeddingRole.QUERY)
        await embmod.generate_embeddings(["a document"])

        assert fake.seen == [["machine learning"], ["a document"]]


class TestQueryCallSiteIsMarked:
    def test_vector_search_passes_the_query_role(self):
        """Pins the one call site in the codebase that embeds a query.

        Without this, dropping `role=` here compiles fine and nothing fails.
        """
        from pathlib import Path

        source = Path("open_notebook/domain/notebook.py").read_text()
        start = source.index("async def vector_search(")
        body = source[start : source.index("\nasync def ", start + 10)]

        assert "role=EmbeddingRole.QUERY" in body, (
            "vector_search embeds the user's query; without the query role an "
            "E5-family model would embed it as a passage"
        )

    def test_document_paths_keep_the_passage_default(self):
        from pathlib import Path

        source = Path("commands/embedding_commands.py").read_text()

        # Documents rely on the default. Assert no call site overrides it to
        # QUERY, which would mean stored content was being embedded as a query.
        assert "role=EmbeddingRole.QUERY" not in source
