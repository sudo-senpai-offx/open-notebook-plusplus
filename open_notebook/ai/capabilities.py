# open_notebook/ai/capabilities.py
from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

TaskType = Literal[
    "extraction",
    "chunking",
    "transformation",
    "embedding",
    "rag_query",
    "reasoning",
    "chat",
    "tts",
    "stt",
    "ocr",
    "web_extraction",
    "code_chunking",
]


@dataclass(frozen=True)
class HardwareInfo:
    platform: str
    cpu_cores_logical: int
    cpu_cores_physical: int
    total_ram_mb: int
    available_ram_mb: int
    has_avx2: bool
    is_container: bool
    cpu_limit_cpus: Optional[float] = None
    mem_limit_mb: Optional[int] = None


@dataclass(frozen=True)
class ModelCandidate:
    name: str
    provider: str
    size_mb: int = 0
    quant: Optional[str] = None
    context_tokens: int = 4096
    embedding: bool = False
    reranker: bool = False
    tts: bool = False
    stt: bool = False
    multimodal: bool = False
    cpu_friendly: float = 0.5
    quality: float = 0.5
    local: bool = True
    supports_onnx: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TaskBudget:
    time_ms_p90: int
    peak_ram_mb: int
    prefer_local: bool = True
    allow_cloud: bool = False
    max_concurrency: int = 1
    min_quality: float = 0.6


@dataclass(frozen=True)
class RoutingDecision:
    task: TaskType
    primary: Optional[ModelCandidate] = None
    fallback_chain: tuple[ModelCandidate, ...] = ()
    mixture: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    use_mixture: bool = False
    time_budget_ms: int = 0
    peak_ram_mb: int = 0


_TASK_BUDGETS: Dict[TaskType, TaskBudget] = {
    "extraction": TaskBudget(time_ms_p90=1000, peak_ram_mb=2048, max_concurrency=1, min_quality=0.7),
    "web_extraction": TaskBudget(time_ms_p90=500, peak_ram_mb=512, max_concurrency=1, min_quality=0.7),
    "ocr": TaskBudget(time_ms_p90=2000, peak_ram_mb=1024, max_concurrency=1, min_quality=0.7, allow_cloud=False),
    "chunking": TaskBudget(time_ms_p90=200, peak_ram_mb=256, max_concurrency=2, min_quality=0.8),
    "code_chunking": TaskBudget(time_ms_p90=200, peak_ram_mb=256, max_concurrency=2, min_quality=0.85),
    "embedding": TaskBudget(time_ms_p90=800, peak_ram_mb=1536, max_concurrency=1, min_quality=0.8),
    "transformation": TaskBudget(time_ms_p90=3000, peak_ram_mb=1024, max_concurrency=1, min_quality=0.65),
    "rag_query": TaskBudget(time_ms_p90=500, peak_ram_mb=512, max_concurrency=2, min_quality=0.75),
    "reasoning": TaskBudget(time_ms_p90=4000, peak_ram_mb=1024, max_concurrency=1, min_quality=0.65),
    "chat": TaskBudget(time_ms_p90=2000, peak_ram_mb=1024, max_concurrency=1, min_quality=0.65),
    "tts": TaskBudget(time_ms_p90=1500, peak_ram_mb=1024, max_concurrency=1, min_quality=0.7),
    "stt": TaskBudget(time_ms_p90=1500, peak_ram_mb=1024, max_concurrency=1, min_quality=0.7),
}


_LOCAL_BASE: List[ModelCandidate] = [
    ModelCandidate(name="llama3.2:3b", provider="ollama", size_mb=2000, quant="Q4_K_M", context_tokens=8192, cpu_friendly=0.95, quality=0.72, local=True),
    ModelCandidate(name="llama3.2:1b", provider="ollama", size_mb=800, quant="Q4_K_M", context_tokens=4096, cpu_friendly=0.98, quality=0.60, local=True),
    ModelCandidate(name="qwen2.5:3b", provider="ollama", size_mb=2000, quant="Q4_K_M", context_tokens=8192, cpu_friendly=0.94, quality=0.74, local=True),
    ModelCandidate(name="qwen2.5:1.5b", provider="ollama", size_mb=1000, quant="Q4_K_M", context_tokens=8192, cpu_friendly=0.97, quality=0.65, local=True),
    ModelCandidate(name="smollm2:360m", provider="ollama", size_mb=250, quant="Q4_K_M", context_tokens=2048, cpu_friendly=0.99, quality=0.55, local=True),
    ModelCandidate(name="bge-m3", provider="ollama", size_mb=600, quant="fp16", context_tokens=8192, embedding=True, cpu_friendly=0.90, quality=0.85, local=True),
    ModelCandidate(name="mxbai-rerank-xsmall-v1", provider="onnx", size_mb=244, quant="int8", context_tokens=512, reranker=True, supports_onnx=True, cpu_friendly=0.98, quality=0.88, local=True),
    ModelCandidate(name="bge-reranker-base", provider="onnx", size_mb=278, quant="fp16", context_tokens=512, reranker=True, supports_onnx=True, cpu_friendly=0.96, quality=0.87, local=True),
    ModelCandidate(name="llava:7b", provider="ollama", size_mb=4600, quant="Q4_K_M", context_tokens=4096, multimodal=True, cpu_friendly=0.60, quality=0.78, local=True),
]


def get_hardware() -> HardwareInfo:
    return _detect_hardware()


def get_task_budgets() -> Dict[TaskType, TaskBudget]:
    return _TASK_BUDGETS


def get_local_candidates() -> List[ModelCandidate]:
    return list(_LOCAL_BASE)


def estimate_ram_for_candidate(c: ModelCandidate, ctx_tokens: int) -> int:
    return _estimate_peak_ram_mb(c, ctx_tokens)


def _estimate_peak_ram_mb(candidate: ModelCandidate, ctx_tokens: int) -> int:
    base = candidate.size_mb
    kv_overhead_mb = max(32, int(ctx_tokens * 0.05))
    return base + kv_overhead_mb + 256


def _score_candidate(hw: HardwareInfo, task: TaskBudget, c: ModelCandidate, ctx_tokens: int) -> float:
    peak = _estimate_peak_ram_mb(c, ctx_tokens)
    ram_ok = 1.0 if peak <= hw.available_ram_mb else max(0.0, 1.0 - (peak - hw.available_ram_mb) / max(512, hw.available_ram_mb))
    cpu_ok = c.cpu_friendly
    local_pref = 1.05 if (c.local and task.prefer_local) else 1.0
    cloud_pen = 0.90 if (not c.local and not task.allow_cloud) else 1.0
    score = c.quality * cpu_ok * ram_ok * local_pref * cloud_pen
    return round(score, 4)


def pick_best_for_task(task: TaskType, ctx_tokens: int = 4096, allow_cloud: Optional[bool] = None) -> RoutingDecision:
    hw = _detect_hardware()
    budgets = _TASK_BUDGETS
    budget = budgets.get(task, TaskBudget(time_ms_p90=2000, peak_ram_mb=1024, max_concurrency=1, min_quality=0.6))
    if allow_cloud is not None:
        budget = TaskBudget(
            time_ms_p90=budget.time_ms_p90,
            peak_ram_mb=budget.peak_ram_mb,
            prefer_local=budget.prefer_local,
            allow_cloud=allow_cloud,
            max_concurrency=budget.max_concurrency,
            min_quality=budget.min_quality,
        )

    candidates = list(_LOCAL_BASE)
    scored: List[tuple[float, ModelCandidate]] = []
    for c in candidates:
        s = _score_candidate(hw, budget, c, ctx_tokens)
        scored.append((s, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    primary = None
    chain: List[ModelCandidate] = []
    for s, c in scored:
        peak = _estimate_peak_ram_mb(c, ctx_tokens)
        if s < budget.min_quality:
            continue
        if peak > hw.available_ram_mb * 1.25:
            continue
        if primary is None:
            primary = c
        else:
            chain.append(c)
        if len(chain) >= 3:
            break

    reason = f"hw={hw.cpu_cores_logical}c/{hw.total_ram_mb}MB avail={hw.available_ram_mb}MB task={task}"
    use_mixture = task in ("transformation", "reasoning", "chat", "rag_query")
    mixture: Dict[str, Any] = {}
    if use_mixture:
        mixture = {
            "strategy": "map_reduce" if task == "transformation" else "classifier_fanout",
            "max_concurrency": budget.max_concurrency,
            "adaptive_batches": True,
        }

    return RoutingDecision(
        task=task,
        primary=primary,
        fallback_chain=tuple(chain),
        mixture=mixture,
        reason=reason,
        use_mixture=use_mixture,
        time_budget_ms=budget.time_ms_p90,
        peak_ram_mb=budget.peak_ram_mb,
    )


def _detect_hardware() -> HardwareInfo:
    """Detect hardware characteristics."""
    import os
    import platform
    try:
        import psutil
    except Exception:
        psutil = None

    cpu_cores_logical = os.cpu_count() or 1
    cpu_cores_physical = 1
    if psutil:
        try:
            cpu_cores_physical = psutil.cpu_count(logical=False) or cpu_cores_logical
        except Exception:
            cpu_cores_physical = cpu_cores_logical

    total_ram_mb = 4096
    available_ram_mb = 2048
    if psutil:
        try:
            vm = psutil.virtual_memory()
            total_ram_mb = int(vm.total / (1024 * 1024))
            available_ram_mb = int(vm.available / (1024 * 1024))
        except Exception:
            pass

    has_avx2 = False
    try:
        import sys
        # Simple AVX2 detection via /proc/cpuinfo if available
        if os.path.exists('/proc/cpuinfo'):
            with open('/proc/cpuinfo', 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if 'avx2' in line.lower():
                        has_avx2 = True
                        break
    except Exception:
        pass

    is_container = False
    try:
        if os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv'):
            is_container = True
    except Exception:
        pass

    cpu_limit_cpus = None
    try:
        # cgroup v2
        if os.path.exists('/sys/fs/cgroup/cpu.max'):
            with open('/sys/fs/cgroup/cpu.max', 'r') as f:
                parts = f.read().strip().split()
                if len(parts) == 2 and parts[0].isdigit():
                    cpu_limit_cpus = float(parts[0]) / float(parts[1]) if float(parts[1]) > 0 else None
    except Exception:
        pass

    mem_limit_mb = None
    try:
        if os.path.exists('/sys/fs/cgroup/memory.max'):
            with open('/sys/fs/cgroup/memory.max', 'r') as f:
                v = f.read().strip()
                if v.isdigit():
                    mem_limit_mb = int(int(v) / (1024 * 1024))
    except Exception:
        pass

    if cpu_limit_cpus is not None:
        cpu_cores_logical = int(cpu_limit_cpus) if cpu_limit_cpus >= 1 else max(1, int(cpu_limit_cpus * cpu_cores_logical))
    if mem_limit_mb is not None:
        available_ram_mb = min(available_ram_mb, mem_limit_mb)
        total_ram_mb = min(total_ram_mb, mem_limit_mb)

    return HardwareInfo(
        platform=platform.system().lower(),
        cpu_cores_logical=max(1, cpu_cores_logical),
        cpu_cores_physical=max(1, cpu_cores_physical),
        total_ram_mb=max(512, total_ram_mb),
        available_ram_mb=max(256, available_ram_mb),
        has_avx2=has_avx2,
        is_container=is_container,
        cpu_limit_cpus=cpu_limit_cpus,
        mem_limit_mb=mem_limit_mb,
    )
