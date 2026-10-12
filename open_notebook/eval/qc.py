"""Retrieval QC scoring (N2b). Pure functions over result id lists.

A hit requires ALL expected ids in the top-k (strict: partial credit hides
regressions on multi-hop questions).
"""

from __future__ import annotations

from typing import Any, Dict, List


def _ids(results: List[Dict[str, Any]]) -> List[str]:
    return [str(r.get("id")) for r in results or []]


def recall_at_k(
    results: List[Dict[str, Any]], expected_ids: List[str], k: int
) -> float:
    """1.0 when every expected id appears in the top-k, else 0.0."""
    top = _ids(results)[: max(0, k)]
    if not expected_ids:
        return 0.0
    return 1.0 if all(e in top for e in expected_ids) else 0.0


def summarize(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-question {question, hit} outcomes."""
    hits = sum(1 for o in outcomes if o.get("hit"))
    total = len(outcomes)
    return {"total": total, "hits": hits, "recall": (hits / total) if total else 0.0}
