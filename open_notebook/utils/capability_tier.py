"""Capability-tier gate for training features on constrained hardware.

Decides whether a training feature may run on this machine *right now*,
using live signals instead of static config. Unknown memory fails safe
(LoRA denied): an admitted-then-OOMed finetune would poison the single
worker queue with `stale: no heartbeat` churn.

Thresholds (locked by T2 spec, revisit after T5 prototype measurements):
LoRA on bases <=0.5B needs >=3072 MB free and >=2 threads. Tiny
from-scratch (sub-20M) and tabular stay open on the 2GB tier.
"""

from __future__ import annotations

import os
import platform

from loguru import logger

LORA_MIN_MEM_MB = 3072
LORA_MIN_CPUS = 2

_LIGHT_FEATURES = frozenset({"tiny_train", "tabular"})


def _parse_memavailable_mb(text: str) -> int | None:
    """Parse `MemAvailable` (kB) from /proc/meminfo text into MB."""
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                kb = int(line.split()[1])
            except (IndexError, ValueError):
                return None
            return kb // 1024
    return None


def mem_available_mb() -> int | None:
    """Free memory in MB, or None when it cannot be determined."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            return _parse_memavailable_mb(f.read())
    except OSError:
        return None


def cpu_count() -> int:
    """Usable thread count; unknown counts as 1 (fails safe)."""
    return os.cpu_count() or 1


def isa_note() -> str:
    """Short ISA tag for ladder decisions. Logged, never gating in v1."""
    machine = platform.machine() or "unknown"
    flags: set[str] = set()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("flags") or line.startswith("Features"):
                    flags.update(line.split(":")[-1].split())
                    break
    except OSError:
        pass
    for token in ("avx2", "avx", "ssse3", "sse3", "sse2", "neon", "asimd"):
        if token in flags:
            return f"{machine}:{token}"
    return machine


def tier_allows(feature: str) -> tuple[bool, str]:
    """Whether ``feature`` may run here. Returns (allowed, reason)."""
    if feature in _LIGHT_FEATURES:
        mem = mem_available_mb()
        label = f"{mem} MB free" if mem is not None else "memory unknown"
        return True, f"{feature} is open on the 2GB tier ({label}, {isa_note()})."
    if feature == "lora":
        mem = mem_available_mb()
        cpus = cpu_count()
        if mem is None:
            return (
                False,
                "LoRA needs >=3072 MB free memory but it could not be "
                f"determined on this machine ({isa_note()}, {cpus} threads).",
            )
        if mem < LORA_MIN_MEM_MB or cpus < LORA_MIN_CPUS:
            return (
                False,
                f"LoRA needs >=3072 MB free and >=2 threads; this machine "
                f"has {mem} MB free and {cpus} threads ({isa_note()}). "
                "Free memory or use tiny/tabular training instead.",
            )
        logger.debug(f"Tier gate open for lora: {mem} MB free, {cpus} threads.")
        return True, f"LoRA unlocked ({mem} MB free, {cpus} threads)."
    raise ValueError(f"unknown capability feature: {feature!r}")
