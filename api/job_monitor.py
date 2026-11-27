"""Job monitor — reduced to departure event emission only.

Reaping logic (stale running jobs → failed) has been merged into the
dispatcher loop (_reap_stale_jobs in dispatcher.py) with verbatim criteria:
stale running jobs >300s, confirmed twice before marking failed, never touches
held jobs. JobMonitor now only detects departed jobs (left new/running between
passes) and emits completion/failed events.
"""

import asyncio
import collections.abc
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Set

from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.utils.notify import JobEvent, notify_job_event

__all__ = ["JobMonitor"]


class JobMonitor:
    def __init__(
        self,
        notify: Callable[
            [JobEvent, Dict], collections.abc.Awaitable[None]
        ] = notify_job_event,
        interval: Optional[float] = None,
    ) -> None:
        self._notify = notify
        self._interval = interval if interval is not None else 60
        self._tracked: Set[str] = set()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def _fetch_active(self) -> list:
        try:
            return await repo_query(
                "SELECT id, app, name, status, heartbeat_at, error_message, args FROM command WHERE status = 'new' OR status = 'running' LIMIT 2000",
            )
        except Exception as e:
            logger.debug(f"job_monitor: fetch_active failed: {e}")
            return []

    async def _fetch_one(self, job_id: str) -> Optional[Dict]:
        try:
            rid = job_id
            if ":" not in rid:
                rid = f"command:{rid}"
            rows = await repo_query("SELECT * FROM $id", {"id": ensure_record_id(rid)})
            return rows[0] if rows else None
        except Exception as e:
            logger.debug(f"job_monitor: fetch_one failed for {job_id}: {e}")
            return None

    async def _emit(self, row: Dict) -> None:
        try:
            status = row.get("status")
            event: JobEvent = "completed" if status == "completed" else "failed"
            subset = {
                k: row.get(k)
                for k in ("id", "app", "name", "status", "error_message", "args")
            }
            await self._notify(event, subset)
        except Exception as e:
            logger.debug(f"job_monitor: emit failed: {e}")

    async def run_once(self, now: Optional[datetime] = None) -> None:
        if now is None:
            now = datetime.now(timezone.utc)
        rows = await self._fetch_active()
        active = {str(r["id"]) for r in rows}

        departed = self._tracked - active
        for job_id in sorted(departed):
            row = await self._fetch_one(job_id)
            if row:
                await self._emit(row)

        self._tracked = active

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.debug(f"job_monitor: run_once error: {e}")
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
