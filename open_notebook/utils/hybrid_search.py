# open_notebook/utils/hybrid_search.py
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from ..ai.model_policy import use_mixture

logger = logging.getLogger(__name__)


def rrf_fuse(
    vector_results: List[Dict[str, Any]],
    bm25_results: List[Dict[str, Any]],
    k: int = 60,
    top_n: int = 20,
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion. Keys expected: 'id' or 'source_id', 'score' or 'rank'."""
    scores: Dict[str, float] = {}
    docs: Dict[str, Dict[str, Any]] = {}

    def add(items: List[Dict[str, Any]], weight: float = 1.0):
        for i, item in enumerate(items):
            rank = i + 1
            doc_id = item.get("id") or item.get("source_id") or item.get("hash") or str(id(item))
            score = item.get("score")
            if score is None and rank > 0:
                score = 1.0 / (k + rank)
            else:
                try:
                    score = float(score) * weight
                except Exception:
                    score = 1.0 / (k + rank)
            if doc_id not in docs:
                docs[doc_id] = dict(item)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank) * weight

    add(vector_results or [], weight=1.0)
    add(bm25_results or [], weight=1.0)

    fused = []
    for doc_id, s in scores.items():
        d = dict(docs[doc_id])
        d["hybrid_score"] = float(s)
        fused.append(d)
    fused.sort(key=lambda x: x["hybrid_score"], reverse=True)
    if top_n and len(fused) > top_n:
        fused = fused[:top_n]
    return fused


def hybrid_enabled() -> bool:
    return use_mixture()
