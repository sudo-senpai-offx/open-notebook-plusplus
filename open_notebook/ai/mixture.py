# open_notebook/ai/mixture.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .capabilities import RoutingDecision
from .router import route_task

logger = logging.getLogger(__name__)

MixtureStrategy = (
    str  # "map_reduce" | "classifier_fanout" | "rerank_fuse" | "synthesize"
)


async def _gather_bounded(
    coros: Sequence[Awaitable[Any]], max_concurrency: int
) -> List[Any]:
    if max_concurrency <= 0:
        max_concurrency = 1
    results: List[Any] = [None] * len(coros)
    sem = asyncio.Semaphore(max_concurrency)

    async def run(i: int, c: Awaitable[Any]) -> None:
        async with sem:
            try:
                results[i] = await c
            except Exception as e:
                # An f-string: this project uses loguru, which does not format
                # %-style arguments, so the placeholders were printed literally
                # and the log said nothing about which task failed or why.
                logger.warning(f"mixture.task_failed idx={i} err={e}")
                results[i] = None

    await asyncio.gather(*(run(i, c) for i, c in enumerate(coros)))
    return results


async def map_reduce(
    chunks: Sequence[str],
    map_fn: Callable[[str, int], Awaitable[Any]],
    reduce_fn: Callable[[List[Any]], Awaitable[Any]],
    max_concurrency: int = 1,
    adaptive_batches: bool = True,
) -> Any:
    if not chunks:
        return await reduce_fn([])
    coros = [map_fn(ch, i) for i, ch in enumerate(chunks)]
    mapped = await _gather_bounded(coros, max_concurrency=max_concurrency)
    mapped_clean = [m for m in mapped if m is not None]
    return await reduce_fn(mapped_clean)


async def fanout(
    items: Sequence[Any],
    fn: Callable[[Any, int], Awaitable[Any]],
    max_concurrency: int = 1,
) -> List[Any]:
    if not items:
        return []
    coros = [fn(it, i) for i, it in enumerate(items)]
    return await _gather_bounded(coros, max_concurrency=max_concurrency)


async def orchestrate(
    task_hint: Optional[str] = None,
    input_shape: Optional[Dict[str, Any]] = None,
    chunks: Optional[Sequence[str]] = None,
    items: Optional[Sequence[Any]] = None,
    map_fn: Optional[Callable[[str, int], Awaitable[Any]]] = None,
    reduce_fn: Optional[Callable[[List[Any]], Awaitable[Any]]] = None,
    item_fn: Optional[Callable[[Any, int], Awaitable[Any]]] = None,
    ctx_tokens: int = 4096,
) -> Tuple[Any, RoutingDecision]:
    rd = route_task(task_hint=task_hint, input_shape=input_shape, ctx_tokens=ctx_tokens)
    strategy = rd.mixture.get("strategy") if rd.mixture else None
    max_c = rd.mixture.get("max_concurrency", 1)
    try:
        max_c = int(max_c)
    except Exception:
        max_c = 1
    if max_c < 1:
        max_c = 1
    adaptive = bool(rd.mixture.get("adaptive_batches", True))

    result: Any = None
    if rd.use_mixture and strategy == "map_reduce" and chunks and map_fn and reduce_fn:
        result = await map_reduce(
            chunks, map_fn, reduce_fn, max_concurrency=max_c, adaptive_batches=adaptive
        )
    elif rd.use_mixture and strategy == "classifier_fanout" and items and item_fn:
        result = await fanout(items, item_fn, max_concurrency=max_c)
    elif rd.use_mixture and strategy == "rerank_fuse":
        result = {"strategy": "rerank_fuse", "items": len(items or [])}
    else:
        result = {
            "strategy": "single",
            "primary": rd.primary.name if rd.primary else None,
        }

    return result, rd
