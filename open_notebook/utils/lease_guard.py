"""Lease guard: veto idle-hibernate while worker jobs are leased.

Single authority is the SurrealDB `command` table (see T3 findings):
a queued (`new`) row or a fresh-heartbeat (`running`, not stale) row blocks
hibernate. A stale `running` row never blocks — its worker is gone and the
JobMonitor marks it failed; counting it would veto hibernate forever after
a crash.

Threshold env (`OPEN_NOTEBOOK_STALE_JOB_SECONDS`, default 300) is read here
rather than imported from `api.job_monitor` to keep the dependency direction
(api depends on open_notebook, never the reverse). The staleness semantics
themselves live once in `open_notebook.utils.job_control` and are shared.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from open_notebook.database.repository import repo_query
from open_notebook.utils.job_control import ACTIVE_STATUSES, is_stale


def _get_stale_threshold() -> int:
    try:
        return int(os.getenv("OPEN_NOTEBOOK_STALE_JOB_SECONDS", "300"))
    except Exception:
        return 300


def blocking_leases(
    rows: List[Dict[str, Any]], now: datetime, threshold_seconds: int
) -> List[Dict[str, Any]]:
    """Rows that veto hibernate: queued or freshly-heartbeating jobs."""
    blocking: List[Dict[str, Any]] = []
    for row in rows:
        status = row.get("status")
        if status not in ACTIVE_STATUSES:
            continue
        if status == "running" and is_stale(row, threshold_seconds, now):
            continue
        blocking.append(row)
    return blocking


async def hibernate_allowed(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Whether idle-hibernate may proceed. Returns (allowed, reason)."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        rows = await repo_query(
            "SELECT id, name, status, heartbeat_at FROM command "
            "WHERE status = 'new' OR status = 'running' LIMIT 2000",
        )
    except Exception as e:
        return False, f"lease state unreadable, refusing hibernate: {e}"
    blocking = blocking_leases(rows or [], now, _get_stale_threshold())
    if blocking:
        names = sorted({str(r.get("name", "?")) for r in blocking})
        return (
            False,
            f"{len(blocking)} active job(s) hold the worker "
            f"({', '.join(names)}); hibernate vetoed by lease guard.",
        )
    return True, "no active jobs; hibernate allowed by lease guard."
