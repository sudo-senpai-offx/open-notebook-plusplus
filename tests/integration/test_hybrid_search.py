"""Week 6 Day 1: Hybrid search with Reciprocal Rank Fusion (RRF) tests.

All 4 tests run without external binaries (no Ollama, no SurrealDB) via
mocks. They exercise:

  * :func:`reciprocal_rank_fusion` — RRF math, disjoint/empty lists,
    deduplication, limit enforcement.
  * :func:`hybrid_search` — parallel FTS + vector execution, FTS-only
    fallback (``search_mode = "fts_fallback"``) when Ollama is unloaded.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.retrieval.search_hybrid import (
    DEFAULT_W_BM25,
    DEFAULT_W_VEC,
    RRF_K,
    hybrid_search,
    reciprocal_rank_fusion,
)


# ── Test 1: RRF boosts documents appearing in both lists ───────────────


def test_rrf_boosts_documents_appearing_in_both_lists():
    """An item ranked #2 in FTS and #2 in Vector outranks an item ranked
    #1 in only one list.

    RRF score for item in both lists (rank 2, rank 2):
        1/(60+2) + 1/(60+2) = 2/62 ≈ 0.0323
    RRF score for item in FTS only (rank 1):
        1/(60+1) = 1/61 ≈ 0.0164
    """
    fts_results = [
        {"id": "chunk:only-fts", "text": "FTS-only chunk"},
        {"id": "chunk:both", "text": "In both lists"},
    ]
    vector_results = [
        {"id": "chunk:only-vec", "text": "Vector-only chunk"},
        {"id": "chunk:both", "text": "In both lists"},
    ]

    merged = reciprocal_rank_fusion(fts_results, vector_results, k=RRF_K, limit=10)

    assert len(merged) == 3
    # "chunk:both" should be first (highest RRF score)
    assert merged[0]["id"] == "chunk:both"
    assert merged[0]["fts_rank"] == 2
    assert merged[0]["vector_rank"] == 2
    assert merged[0]["rrf_score"] == pytest.approx(2.0 / (RRF_K + 2), rel=1e-6)

    # "chunk:only-fts" (rank 1 in FTS only) vs "chunk:only-vec" (rank 1 in
    # Vector only) — same score, tie-broken by id
    assert merged[1]["id"] in ("chunk:only-fts", "chunk:only-vec")
    assert merged[2]["id"] in ("chunk:only-fts", "chunk:only-vec")


# ── Test 2: RRF handles disjoint and empty lists ───────────────────────


def test_rrf_handles_disjoint_and_empty_lists():
    """Deterministic ordering when FTS or Vector returns []."""

    # FTS empty → only vector results, ranked by vector rank
    merged = reciprocal_rank_fusion(
        [], [{"id": "chunk:vec-1", "text": "v1"}], k=RRF_K, limit=10
    )
    assert len(merged) == 1
    assert merged[0]["id"] == "chunk:vec-1"
    assert merged[0]["fts_rank"] is None
    assert merged[0]["vector_rank"] == 1

    # Vector empty → only FTS results
    merged = reciprocal_rank_fusion(
        [{"id": "chunk:fts-1", "text": "f1"}], [], k=RRF_K, limit=10
    )
    assert len(merged) == 1
    assert merged[0]["id"] == "chunk:fts-1"
    assert merged[0]["fts_rank"] == 1
    assert merged[0]["vector_rank"] is None

    # Both empty → no results
    merged = reciprocal_rank_fusion([], [], k=RRF_K, limit=10)
    assert merged == []


# ── Test 3: hybrid_search falls back to FTS when embedding unavailable ─


@pytest.mark.asyncio
async def test_hybrid_search_falls_back_to_fts_when_embedding_unavailable():
    """Simulate Ollama unloaded during batch mode → search still returns
    FTS hits with search_mode == "fts_fallback" instead of raising."""
    fts_hits = [
        {
            "id": "chunk:1",
            "source_id": "source:1",
            "text": "FTS result 1",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 10,
            "page_number": 1,
            "heading_path": None,
            "embedding_model": None,
        },
        {
            "id": "chunk:2",
            "source_id": "source:1",
            "text": "FTS result 2",
            "chunk_index": 1,
            "char_start": 11,
            "char_end": 21,
            "page_number": 1,
            "heading_path": None,
            "embedding_model": None,
        },
    ]

    # Ollama not loaded → skip vector leg
    with patch(
        "open_notebook.retrieval.search_hybrid._check_ollama_loaded",
        AsyncMock(return_value=False),
    ), patch(
        "open_notebook.retrieval.search_hybrid._fts_search",
        AsyncMock(return_value=fts_hits),
    ) as mock_fts:
        result = await hybrid_search(
            keyword="test query",
            results=10,
            mode="hybrid",
        )

    assert result["search_mode"] == "fts_fallback", (
        f"Expected search_mode='fts_fallback', got {result['search_mode']!r}"
    )
    assert result["total"] == 2
    assert len(result["results"]) == 2
    assert result["results"][0]["chunk_id"] == "chunk:1"
    mock_fts.assert_called_once()


# ── Test 4: hybrid_search deduplicates and respects limit ─────────────


@pytest.mark.asyncio
async def test_hybrid_search_deduplicates_and_respects_limit():
    """Duplicate chunk IDs merge into one result with both fts_rank and
    vector_rank populated; limit is respected."""
    fts_hits = [
        {
            "id": "chunk:shared",
            "text": "Shared chunk",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 12,
        },
        {
            "id": "chunk:fts-only",
            "text": "FTS only",
            "chunk_index": 1,
            "char_start": 13,
            "char_end": 20,
        },
    ]
    vector_hits = [
        {
            "id": "chunk:shared",
            "text": "Shared chunk",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 12,
        },
        {
            "id": "chunk:vec-only",
            "text": "Vector only",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 12,
        },
    ]

    with patch(
        "open_notebook.retrieval.search_hybrid._check_ollama_loaded",
        AsyncMock(return_value=True),
    ), patch(
        "open_notebook.retrieval.search_hybrid._fts_search",
        AsyncMock(return_value=fts_hits),
    ), patch(
        "open_notebook.retrieval.search_hybrid._vector_search",
        AsyncMock(return_value=vector_hits),
    ):
        # limit=1 → only the top result
        result = await hybrid_search(
            keyword="test query",
            results=1,
            mode="hybrid",
        )

        assert result["total"] == 1, (
            f"Expected total=1 with limit=1, got {result['total']}"
        )
        assert len(result["results"]) == 1
        # The shared chunk should win (it's in both lists → higher RRF score)
        assert result["results"][0]["chunk_id"] == "chunk:shared"
        assert result["results"][0]["fts_rank"] == 1
        assert result["results"][0]["vector_rank"] == 1
        assert result["results"][0]["rrf_score"] is not None
        assert result["search_mode"] == "hybrid"

    # limit=2 → top 2 results
    with patch(
        "open_notebook.retrieval.search_hybrid._check_ollama_loaded",
        AsyncMock(return_value=True),
    ), patch(
        "open_notebook.retrieval.search_hybrid._fts_search",
        AsyncMock(return_value=fts_hits),
    ), patch(
        "open_notebook.retrieval.search_hybrid._vector_search",
        AsyncMock(return_value=vector_hits),
    ):
        result = await hybrid_search(
            keyword="test query",
            results=2,
            mode="hybrid",
        )

        assert result["total"] == 2
        assert len(result["results"]) == 2
        # Both "chunk:shared" and one other should appear
        chunk_ids = {r["chunk_id"] for r in result["results"]}
        assert "chunk:shared" in chunk_ids
        # The shared chunk has both ranks populated
        shared = [r for r in result["results"] if r["chunk_id"] == "chunk:shared"][0]
        assert shared["fts_rank"] is not None
        assert shared["vector_rank"] is not None
