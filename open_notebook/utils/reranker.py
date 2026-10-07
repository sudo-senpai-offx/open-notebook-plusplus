# open_notebook/utils/reranker.py
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from ..ai.model_policy import use_reranker

logger = logging.getLogger(__name__)


def _load_reranker():
    """Lazy load a small local reranker if available. Returns None if not available."""
    try:
        # Try sentence-transformers CrossEncoder style small model via onnx if installed
        import onnxruntime as ort  # type: ignore
    except Exception:
        ort = None  # type: ignore

    if ort is None:
        return None

    # For now, return a lightweight stub that can be extended later
    # Actual weights integration is opt-in; stub keeps behavior safe
    class _Stub:
        def predict(self, pairs: List[Tuple[str, str]]) -> List[float]:
            # Simple heuristic: prefer longer/more specific text slightly
            scores = []
            for q, c in pairs:
                s = 0.5
                try:
                    s = min(1.0, max(0.0, 0.5 + (len(c) - len(q)) * 1e-4))
                except Exception:
                    s = 0.5
                scores.append(float(s))
            return scores

    return _Stub()


def rerank_before_truncate(
    query: str,
    chunks: List[str],
    max_results: int = 20,
    max_chars_total: Optional[int] = None,
) -> List[str]:
    """Optionally rerank chunks against query and truncate to max_results/max_chars_total.

    Behavior is unchanged unless USE_RERANKER_BEFORE_TRUNCATE is true AND a reranker is available.
    """
    if not use_reranker():
        if max_results and len(chunks) > max_results:
            return chunks[:max_results]
        return list(chunks)

    reranker = _load_reranker()
    if reranker is None:
        logger.debug("reranker.enabled_but_unavailable")
        if max_results and len(chunks) > max_results:
            return chunks[:max_results]
        return list(chunks)

    try:
        pairs = [(query, c) for c in chunks]
        scores = reranker.predict(pairs)
        ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        out = [c for _, c in ranked]
        if max_results and len(out) > max_results:
            out = out[:max_results]
        if max_chars_total:
            total = 0
            trimmed = []
            for c in out:
                if total + len(c) > max_chars_total:
                    break
                trimmed.append(c)
                total += len(c)
            out = trimmed
        return out
    except Exception as e:
        logger.warning("reranker.failed err=%s", e)
        if max_results and len(chunks) > max_results:
            return chunks[:max_results]
        return list(chunks)
