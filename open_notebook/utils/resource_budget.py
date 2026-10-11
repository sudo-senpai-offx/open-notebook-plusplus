"""Resource budgets as percent of detected *total* system resources.

Totals (MemTotal, cpu count) are stable denominators; free memory fluctuates
and is never the denominator. All readers are None-safe: unknown totals mean
"cannot judge", never 0% or 100%. Enforcement stays warn-only (see
RESOURCE_WARN_ONLY); nothing here kills or throttles.
"""

from __future__ import annotations

from typing import Optional


def _parse_memtotal_mb(text: str) -> Optional[int]:
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) // 1024
            except (IndexError, ValueError):
                return None
    return None


def _parse_vmrss_mb(text: str) -> Optional[int]:
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) // 1024
            except (IndexError, ValueError):
                return None
    return None


def mem_total_mb() -> Optional[int]:
    """Total system RAM in MB, or None when undetectable (non-Linux)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            return _parse_memtotal_mb(f.read())
    except OSError:
        return None


def self_rss_mb() -> Optional[int]:
    """Current process RSS in MB, or None when undetectable."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            return _parse_vmrss_mb(f.read())
    except OSError:
        return None


def utilization_pct(used_mb: Optional[int], total_mb: Optional[int]) -> Optional[int]:
    """Used as percent of total, clamped to 0-100. None when unjudgeable."""
    if used_mb is None or total_mb is None or total_mb <= 0 or used_mb < 0:
        return None
    return min(100, (used_mb * 100) // total_mb)


def self_utilization_pct() -> Optional[int]:
    """This process's RSS as percent of total system RAM."""
    return utilization_pct(self_rss_mb(), mem_total_mb())
