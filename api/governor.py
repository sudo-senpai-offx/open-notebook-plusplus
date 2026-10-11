"""Observe-only hibernate governor (T3).

An asyncio task mirroring `JobMonitor`'s lifecycle. Each tick it evaluates
whether the stack *would* hibernate and logs the verdict — it stops nothing.
Stop-order execution and wake are later slices; lifespan wiring is deferred
until the observe output is trusted.

Idle is DB-visible only: no `command` row created or heartbeating within
`HIBERNATE_IDLE_SECONDS`, and no blocking leases. A disabled flag means
dormant, always.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from open_notebook.ai.model_policy import hibernate_enabled, hibernate_idle_seconds
from open_notebook.database.repository import repo_query
from open_notebook.utils.lease_guard import blocking_leases

_RECENT_LIMIT = 25


def _get_tick_seconds() -> int:
    try:
        return int(os.getenv("OPEN_NOTEBOOK_JOB_MONITOR_SECONDS", "60"))
    except Exception:
        return 60


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _last_activity(rows: List[Dict[str, Any]]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for row in rows:
        for key in ("heartbeat_at", "created"):
            ts = _parse_ts(row.get(key))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    return latest


def evaluate(
    *,
    enabled: bool,
    idle_seconds: int,
    rows: List[Dict[str, Any]],
    now: datetime,
    stale_threshold: int = 300,
) -> Dict[str, Any]:
    """Decide what the governor would do. Never acts; returns the verdict."""
    if not enabled:
        return {"action": "dormant", "reason": "hibernate policy disabled"}
    blocking = blocking_leases(rows, now, stale_threshold)
    if blocking:
        names = sorted({str(r.get("name", "?")) for r in blocking})
        return {
            "action": "vetoed",
            "reason": f"{len(blocking)} active job(s): {', '.join(names)}",
            "blocking": len(blocking),
        }
    last = _last_activity(rows)
    if last is None:
        return {"action": "would_hibernate", "reason": "no commands ever recorded"}
    idle_for = (now - last).total_seconds()
    if idle_for >= idle_seconds:
        return {
            "action": "would_hibernate",
            "reason": f"idle for {int(idle_for)}s (>= {idle_seconds}s)",
            "idle_seconds": int(idle_for),
        }
    return {
        "action": "active",
        "reason": f"last activity {int(idle_for)}s ago",
        "idle_seconds": int(idle_for),
    }


async def _fetch_recent() -> list:
    try:
        return await repo_query(
            "SELECT id, name, status, created, heartbeat_at FROM command "
            "ORDER BY created DESC LIMIT $limit",
            {"limit": _RECENT_LIMIT},
        )
    except Exception as e:
        logger.debug(f"governor: fetch_recent failed: {e}")
        return []


class Governor:
    def __init__(self, interval: Optional[float] = None) -> None:
        self._interval = interval if interval is not None else _get_tick_seconds()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def run_once(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            threshold_raw = os.getenv("OPEN_NOTEBOOK_STALE_JOB_SECONDS", "300")
            stale_threshold = int(threshold_raw)
        except Exception:
            stale_threshold = 300
        rows = await _fetch_recent()
        decision = evaluate(
            enabled=hibernate_enabled(),
            idle_seconds=hibernate_idle_seconds(),
            rows=rows or [],
            now=now,
            stale_threshold=stale_threshold,
        )
        logger.info(
            f"governor: {decision['action']} — {decision['reason']} (observe-only)"
        )
        return decision

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.debug(f"governor: run_once error: {e}")
            await asyncio.sleep(self._interval)

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
