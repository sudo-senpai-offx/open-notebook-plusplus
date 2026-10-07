# open_notebook/ai/selector.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .capabilities import (
    HardwareInfo,
    ModelCandidate,
    RoutingDecision,
    TaskType,
    get_hardware,
    get_local_candidates,
    get_task_budgets,
    pick_best_for_task,
)

logger = logging.getLogger(__name__)


def resolve_routing(task: TaskType, ctx_tokens: int = 4096, allow_cloud: Optional[bool] = None) -> RoutingDecision:
    return pick_best_for_task(task=task, ctx_tokens=ctx_tokens, allow_cloud=allow_cloud)


def score_candidates(task: TaskType, ctx_tokens: int = 4096) -> List[Dict[str, Any]]:
    hw = get_hardware()
    budgets = get_task_budgets()
    budget = budgets.get(task)
    out: List[Dict[str, Any]] = []
    for c in get_local_candidates():
        peak = c.size_mb + max(32, int(ctx_tokens * 0.05)) + 256
        ram_ok = 1.0 if budget is None or peak <= hw.available_ram_mb else max(0.0, 1.0 - (peak - hw.available_ram_mb) / max(512, hw.available_ram_mb))
        cpu_ok = c.cpu_friendly
        local_pref = 1.05 if c.local else 1.0
        cloud_pen = 0.90 if (not c.local and budget and not budget.allow_cloud) else 1.0
        score = round(c.quality * cpu_ok * ram_ok * local_pref * cloud_pen, 4)
        out.append(
            {
                "name": c.name,
                "provider": c.provider,
                "local": c.local,
                "score": score,
                "peak_ram_mb": peak,
                "ram_ok": ram_ok >= 0.9,
                "cpu_friendly": c.cpu_friendly,
                "quality": c.quality,
                "embedding": c.embedding,
                "reranker": c.reranker,
                "multimodal": c.multimodal,
            }
        )
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def get_fallback_chain(task: TaskType, ctx_tokens: int = 4096, allow_cloud: Optional[bool] = None) -> List[ModelCandidate]:
    rd = resolve_routing(task, ctx_tokens, allow_cloud)
    return list(rd.fallback_chain)


def should_use_mixture(task: TaskType) -> bool:
    rd = resolve_routing(task)
    return rd.use_mixture


def explain_routing(task: TaskType, ctx_tokens: int = 4096) -> Dict[str, Any]:
    rd = resolve_routing(task, ctx_tokens)
    return {
        "task": rd.task,
        "primary": rd.primary.name if rd.primary else None,
        "fallback_count": len(rd.fallback_chain),
        "use_mixture": rd.use_mixture,
        "mixture": rd.mixture,
        "reason": rd.reason,
        "time_budget_ms": rd.time_budget_ms,
        "peak_ram_mb": rd.peak_ram_mb,
    }
