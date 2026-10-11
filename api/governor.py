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
from typing import Any, Dict, List, Optional, cast

from loguru import logger

from open_notebook.ai import model_policy
from open_notebook.ai.model_policy import hibernate_enabled, hibernate_idle_seconds
from open_notebook.database.repository import repo_query
from open_notebook.domain.service_state import ServiceState
from open_notebook.utils import resource_budget
from open_notebook.utils.lease_guard import blocking_leases

_RECENT_LIMIT = 25
_SUPERVISOR_SOCKET = "/tmp/supervisor.sock"
_SUPERVISOR_TIMEOUT_S = 30


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


class SupervisorRunner:
    """Stop/start supervisord programs. Injectable seam for tests."""

    def __init__(
        self, socket: str = _SUPERVISOR_SOCKET, program: str = "worker"
    ) -> None:
        self._socket = socket
        self._program = program

    async def _ctl(self, action: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "supervisorctl",
            "-s",
            f"unix://{self._socket}",
            action,
            self._program,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=_SUPERVISOR_TIMEOUT_S
            )
        except asyncio.TimeoutError as e:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError(f"supervisorctl {action} timed out") from e
        if proc.returncode != 0:
            raise RuntimeError(f"supervisorctl {action} failed: {err.decode().strip()}")
        return out.decode().strip()

    async def stop(self, program: str = "worker") -> str:
        if program != self._program:
            raise ValueError(f"runner scoped to {self._program!r}, got {program!r}")
        return await self._ctl("stop")

    async def start(self, program: str = "worker") -> str:
        if program != self._program:
            raise ValueError(f"runner scoped to {self._program!r}, got {program!r}")
        return await self._ctl("start")


class Governor:
    def __init__(
        self,
        interval: Optional[float] = None,
        runner: Optional[SupervisorRunner] = None,
    ) -> None:
        self._interval = interval if interval is not None else _get_tick_seconds()
        self._runner = runner if runner is not None else SupervisorRunner()
        self._asleep = False
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
        # T7: warn-only pressure note. Never changes the action; enforcement
        # stays out until triggers are proven.
        pressure = resource_budget.self_utilization_pct()
        warn_at = model_policy.resource_warn_rss_pct()
        if pressure is not None and pressure >= warn_at:
            decision["pressure_pct"] = pressure
            decision["reason"] += (
                f" | memory pressure {pressure}% >= warn line {warn_at}%"
                " (warn-only, no action)"
            )
        logger.info(
            f"governor: {decision['action']} — {decision['reason']} (observe-only)"
        )
        if decision["action"] == "would_hibernate" and not self._asleep:
            await self._sleep()
        return decision

    async def _sleep(self) -> None:
        """Set draining, then stop the worker. Rolls the flag back if the
        stop fails so the flag never claims a sleep that did not happen."""
        state = cast(ServiceState, await ServiceState.get_instance())
        await state.set_draining(True)
        try:
            await self._runner.stop("worker")
        except Exception:
            try:
                await state.set_draining(False)
            except Exception as e:
                logger.debug(f"governor: draining rollback failed: {e}")
            raise
        self._asleep = True
        logger.info("governor: worker asleep (draining set)")

    async def wake(self) -> None:
        """Restart the worker and clear draining."""
        await self._runner.start("worker")
        state = cast(ServiceState, await ServiceState.get_instance())
        await state.set_draining(False)
        self._asleep = False
        logger.info("governor: worker awake (draining cleared)")

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
