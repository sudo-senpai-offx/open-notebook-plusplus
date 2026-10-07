# open_notebook/utils/adaptive_batching.py
from __future__ import annotations

import logging
from typing import List, TypeVar

from ..ai.capabilities import get_hardware
from ..ai.model_policy import use_mixture

logger = logging.getLogger(__name__)

T = TypeVar("T")


def adaptive_batch_size(
    items: int, base: int = 50, min_b: int = 10, max_b: int = 200
) -> int:
    if items <= 0:
        return base
    hw = get_hardware()
    avail = hw.available_ram_mb
    # Gentle downscale on low RAM (14GB class target; conservative)
    if avail < 4096:
        base_eff = max(min_b, base - 20)
    elif avail < 6144:
        base_eff = max(min_b, base - 10)
    else:
        base_eff = base
    if use_mixture():
        base_eff = min(max_b, base_eff + 10)
    # Keep batches reasonable for CPU
    b = base_eff
    if items < b:
        b = max(min_b, items)
    return max(min_b, min(max_b, b))


def batch_items(items: List[T], base: int = 50) -> List[List[T]]:
    if not items:
        return []
    b = adaptive_batch_size(len(items), base=base)
    batches: List[List[T]] = []
    for i in range(0, len(items), b):
        batches.append(items[i : i + b])
    logger.debug(
        "batching.adaptive count=%s batch_size=%s batches=%s",
        len(items),
        b,
        len(batches),
    )
    return batches
