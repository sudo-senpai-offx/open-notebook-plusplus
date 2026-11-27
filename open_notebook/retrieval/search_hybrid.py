"""Hybrid search with Reciprocal Rank Fusion (RRF) over ``source_chunk``.

This module provides a self-contained hybrid search implementation that is
independent of the :mod:`open_notebook.domain.notebook` search functions.
It exists so the Week 6 frontend (queue backlog panel, partial-source UI,
hybrid search results) can import a clean, tested search surface without
pulling in the entire notebook domain module.

Public surface:
  * :func:`reciprocal_rank_fusion` — merge keyword (FTS/BM25) and vector
    search results using the standard RRF formula.
  * :func:`hybrid_search` — run FTS and vector search in parallel via
    ``asyncio.gather(..., return_exceptions=True)``, merge with RRF, and
    gracefully degrade to FTS-only (``search_mode = "fts_fallback"``) when
    Ollama is unloaded or embedding generation fails.

Design notes
------------
* RRF score per unique chunk id::

    score(d) = sum_{r in {fts, vector}} w_r / (k + rank_r(d))

  where ranks are 1-based.  ``k`` defaults to 60 (the standard value from
  Cormack et al., 2009).
* ``warnings``/``timed_out_pages``/``status`` on the Source record (Week 5
  Day 5) are surfaced through :class:`api.models.SourceResponse` so the
  Week 6 UI can render partial-extraction warnings and retry buttons.
* All SurrealDB access goes through :func:`open_notebook.database.repository.repo_query`,
  so this module can be unit-tested with a monkeypatched ``repo_query``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Literal, Optional

from loguru import logger

from open_notebook.database.repository import repo_query


# ── RRF constants ──────────────────────────────────────────────────────

RRF_K = 60
"""Standard RRF constant (Cormack et al., 2009)."""

DEFAULT_W_BM25 = 1.0
"""Default weight for the FTS/BM25 leg in RRF."""

DEFAULT_W_VEC = 1.0
"""Default weight for the vector leg in RRF."""

DEFAULT_SIMILARITY_THRESHOLD = 0.62
"""Minimum cosine similarity for the vector leg."""

MAX_CHUNKS_HARD_CAP = 50
"""Hard ceiling on results returned to the caller."""


# ── Reciprocal Rank Fusion ─────────────────────────────────────────────


def reciprocal_rank_fusion(
    keyword_results: List[Dict[str, Any]],
    vector_results: List[Dict[str, Any]],
    k: int = RRF_K,
    limit: int = 10,
    fts_weight: float = DEFAULT_W_BM25,
    vector_weight: float = DEFAULT_W_VEC,
) -> List[Dict[str, Any]]:
    """Merge keyword (FTS/BM25) and vector search results using RRF.

    For each unique chunk id, the RRF score is::

        score(d) = w_fts / (k + rank_fts(d)) + w_vec / (k + rank_vec(d))

    Ranks are 1-based.  A chunk appearing in only one leg gets a single
    contribution; a chunk in both legs gets the sum.

    Args:
        keyword_results: FTS/BM25 results, each dict must carry an ``id``
            (or ``chunk_id``) field.  List order determines rank.
        vector_results: Vector results, same shape.
        k: RRF constant (default 60).
        limit: Maximum number of merged results to return.
        fts_weight: Weight for the FTS leg (default 1.0).
        vector_weight: Weight for the vector leg (default 1.0).

    Returns:
        List of merged result dicts sorted by RRF score descending.  Each
        dict carries ``rrf_score``, ``fts_rank`` (1-based or None), and
        ``vector_rank`` (1-based or None) for observability.
    """
    scores: Dict[str, Dict[str, Any]] = {}

    for rank, item in enumerate(keyword_results, start=1):
        cid = str(item.get("id") or item.get("chunk_id") or "")
        if not cid:
            continue
        if cid not in scores:
            merged_item = dict(item)
            merged_item["rrf_score"] = 0.0
            merged_item["fts_rank"] = rank
            merged_item["vector_rank"] = None
            scores[cid] = merged_item
        scores[cid]["rrf_score"] += fts_weight * (1.0 / (k + rank))

    for rank, item in enumerate(vector_results, start=1):
        cid = str(item.get("id") or item.get("chunk_id") or "")
        if not cid:
            continue
        if cid not in scores:
            merged_item = dict(item)
            merged_item["rrf_score"] = 0.0
            merged_item["fts_rank"] = None
            merged_item["vector_rank"] = rank
            scores[cid] = merged_item
        scores[cid]["rrf_score"] += vector_weight * (1.0 / (k + rank))
        scores[cid]["vector_rank"] = rank

    merged = sorted(scores.values(), key=lambda x: -x["rrf_score"])
    return merged[:limit]


# ── Internal helpers ───────────────────────────────────────────────────


def _scope_record_ids(
    notebook_ids: Optional[List[str]],
) -> Optional[List[Any]]:
    """Normalize notebook ids to SurrealDB RecordIDs.

    Returns None for an empty/None scope (global search).
    """
    if not notebook_ids:
        return None
    from open_notebook.database.repository import ensure_record_id

    return [ensure_record_id(nb_id) for nb_id in notebook_ids]


async def _check_ollama_loaded() -> bool:
    """Best-effort check whether Ollama has a model resident in RAM.

    Returns True when Ollama is reachable and reports a loaded model,
    False otherwise (unreachable, unloaded, or any error).  Callers treat
    False as "proceed with FTS-only search".
    """
    try:
        from open_notebook.scheduler.residency import HttpOllamaResidency

        residency = HttpOllamaResidency()
        return await residency.is_loaded()
    except Exception:  # noqa: BLE001 - best effort, never blocks search
        logger.debug("Ollama residency check failed; assuming not loaded")
        return False


async def _fts_search(
    keyword: str,
    results: int,
    accessible_source_ids: Optional[List[Any]],
) -> List[Dict[str, Any]]:
    """Run SurrealDB full-text search (@@) on ``source_chunk.text``.

    Uses the search parser for phrase/boolean/wildcard support.
    """
    from open_notebook.domain.search_parser import parse_query, to_surreal_fts

    parsed = parse_query(keyword)
    fts_clause = to_surreal_fts(parsed, field="text")

    rows = (
        await repo_query(
            """
            SELECT
                id, source_id, text, chunk_index, char_start, char_end,
                page_number, heading_path, embedding_model, index_generation,
                score::float as bm25_score
            FROM source_chunk
            WHERE """ + fts_clause + """
              AND index_generation = (SELECT VALUE active_generation FROM source WHERE id = source_chunk.source_id)
              AND ($accessible_source_ids = NONE OR source_id IN $accessible_source_ids)
              AND (SELECT VALUE searchable FROM source WHERE id = source_chunk.source_id) = true
            ORDER BY score::float DESC
            LIMIT $limit
            """,
            {
                "limit": results * 4,
                "accessible_source_ids": accessible_source_ids,
            },
        )
        or []
    )
    return [dict(r) for r in rows]


async def _vector_search(
    keyword: str,
    results: int,
    accessible_source_ids: Optional[List[Any]],
    similarity_threshold: float,
) -> List[Dict[str, Any]]:
    """Run SurrealDB vector similarity (<|K|>) on ``source_chunk.embedding``.

    Embeds the query via :func:`open_notebook.utils.embedding.generate_embedding`
    with ``role=QUERY``.
    """
    from open_notebook.ai.models import model_manager
    from open_notebook.utils.embedding import generate_embedding
    from open_notebook.utils.embedding_prefix import EmbeddingRole

    emb_model = await model_manager.get_embedding_model()
    if emb_model is None:
        raise RuntimeError("no embedding model configured")

    embed = await generate_embedding(keyword, role=EmbeddingRole.QUERY)

    rows = (
        await repo_query(
            """
            SELECT
                id, source_id, text, chunk_index, char_start, char_end,
                page_number, heading_path, embedding_model, index_generation,
                vector_similarity(embedding, $embed) as cosine_score
            FROM source_chunk
            WHERE embedding != NONE
              AND index_generation = (SELECT VALUE active_generation FROM source WHERE id = source_chunk.source_id)
              AND vector_similarity(embedding, $embed) >= $threshold
              AND ($accessible_source_ids = NONE OR source_id IN $accessible_source_ids)
              AND (SELECT VALUE searchable FROM source WHERE id = source_chunk.source_id) = true
            ORDER BY cosine_score DESC
            LIMIT $limit
            """,
            {
                "embed": embed,
                "threshold": similarity_threshold,
                "limit": results * 4,
                "accessible_source_ids": accessible_source_ids,
            },
        )
        or []
    )
    return [dict(r) for r in rows]


# ── Public hybrid search ───────────────────────────────────────────────


async def hybrid_search(
    keyword: str,
    results: int = 20,
    notebook_ids: Optional[List[str]] = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    fts_weight: float = DEFAULT_W_BM25,
    vector_weight: float = DEFAULT_W_VEC,
    mode: Literal["hybrid", "vector", "text"] = "hybrid",
) -> Dict[str, Any]:
    """Hybrid search over ``source_chunk`` using Reciprocal Rank Fusion.

    Runs FTS and vector search **in parallel** via
    ``asyncio.gather(..., return_exceptions=True)``.  When Ollama is
    unloaded (batch mode active) or embedding generation fails, degrades
    gracefully to FTS-only results with ``search_mode = "fts_fallback"``
    instead of failing the caller's search/chat request.

    Args:
        keyword: Search query string.
        results: Maximum number of results to return (hard cap 50).
        notebook_ids: Optional list of notebook ids to scope the search.
        similarity_threshold: Minimum cosine similarity for the vector leg.
        fts_weight: Weight for the FTS leg in RRF.
        vector_weight: Weight for the vector leg in RRF.
        mode: ``"hybrid"`` (RRF merge), ``"vector"`` (vector only), or
            ``"text"`` (FTS only).

    Returns:
        Dict with keys: ``query``, ``search_mode``, ``total``, ``results``
        (list of chunk dicts).  Each chunk dict carries ``rrf_score``,
        ``fts_rank``, and ``vector_rank`` when in hybrid mode.
    """
    results = min(results, MAX_CHUNKS_HARD_CAP)
    if not keyword or not keyword.strip():
        return {
            "query": keyword,
            "search_mode": mode,
            "total": 0,
            "results": [],
        }

    accessible_source_ids = _scope_record_ids(notebook_ids)
    search_mode = mode

    # ── Decide which legs to run ──────────────────────────────────────

    run_fts = mode in ("hybrid", "text")
    run_vector = mode in ("hybrid", "vector")

    # Quick Ollama residency check before attempting vector search.
    # If Ollama is unloaded, skip the vector leg entirely to avoid a
    # long embedding-generation timeout.
    if run_vector and not await _check_ollama_loaded():
        run_vector = False
        if mode == "hybrid":
            search_mode = "fts_fallback"
        logger.debug("Ollama not loaded; hybrid search degrading to FTS-only")

    # ── Run enabled legs in parallel ──────────────────────────────────

    fts_results: List[Dict[str, Any]] = []
    vector_results: List[Dict[str, Any]] = []

    if run_fts or run_vector:
        fts_coro = _fts_search(keyword, results, accessible_source_ids) if run_fts else None
        vec_coro = (
            _vector_search(keyword, results, accessible_source_ids, similarity_threshold)
            if run_vector
            else None
        )

        gathered = await asyncio.gather(
            *[c for c in (fts_coro, vec_coro) if c is not None],
            return_exceptions=True,
        )

        idx = 0
        if run_fts:
            fts_result = gathered[idx]
            idx += 1
            if isinstance(fts_result, Exception):
                logger.warning(f"FTS leg failed: {fts_result}")
                fts_results = []
            else:
                fts_results = fts_result

        if run_vector:
            vec_result = gathered[idx]
            if isinstance(vec_result, Exception):
                logger.warning(f"Vector leg failed: {vec_result}")
                vector_results = []
                if mode == "hybrid":
                    search_mode = "fts_fallback"
            else:
                vector_results = vec_result

    # ── Merge results ─────────────────────────────────────────────────

    if mode == "hybrid" and fts_results and vector_results:
        merged = reciprocal_rank_fusion(
            fts_results,
            vector_results,
            k=RRF_K,
            limit=results,
            fts_weight=fts_weight,
            vector_weight=vector_weight,
        )
        search_mode = "hybrid"
    elif fts_results:
        merged = fts_results[:results]
        if mode == "hybrid":
            search_mode = "fts_fallback" if not vector_results else "hybrid"
        else:
            search_mode = "text"
    elif vector_results:
        merged = vector_results[:results]
        search_mode = "vector"
    else:
        merged = []
        if mode == "hybrid":
            search_mode = "fts_fallback"

    # Normalize result shape
    normalized: List[Dict[str, Any]] = []
    for item in merged:
        normalized.append(
            {
                "chunk_id": str(item.get("id") or item.get("chunk_id") or ""),
                "source_id": str(item.get("source_id"))
                if item.get("source_id")
                else None,
                "text": item.get("text", ""),
                "chunk_index": item.get("chunk_index", 0),
                "char_start": item.get("char_start", 0),
                "char_end": item.get("char_end", 0),
                "page_number": item.get("page_number"),
                "heading_path": item.get("heading_path"),
                "embedding_model": item.get("embedding_model"),
                "searchable": True,
                "rrf_score": item.get("rrf_score"),
                "fts_rank": item.get("fts_rank"),
                "vector_rank": item.get("vector_rank"),
                "search_mode": search_mode,
            }
        )

    return {
        "query": keyword.strip(),
        "search_mode": search_mode,
        "total": len(normalized),
        "results": normalized,
    }
