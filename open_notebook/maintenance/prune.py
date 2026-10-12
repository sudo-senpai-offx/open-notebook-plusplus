"""Event-log retention (P8): export-then-prune, never delete blind.

Covered tables: revlog + focus_session (event streams, rewritten as JSONL
before deletion). The command table is job history, not event log — never
touched here.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from open_notebook.database.repository import repo_query

PRUNE_TABLES = ("revlog", "focus_session")
DEFAULT_RETENTION_DAYS = 365
MIN_RETENTION_DAYS = 30


def retention_days() -> int:
    try:
        value = int(os.getenv("OPEN_NOTEBOOK_LOG_RETENTION_DAYS", "365"))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(MIN_RETENTION_DAYS, value)


def cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def export_and_prune(table: str, cutoff: str, export_dir: str) -> Dict[str, Any]:
    """Export rows older than cutoff to JSONL, then delete them. Returns
    counts + file name (None when nothing matched)."""
    if table not in PRUNE_TABLES:
        raise ValueError(f"refusing to prune unmanaged table: {table}")
    rows: List[Dict[str, Any]] = (
        await repo_query(
            f"SELECT * FROM {table} WHERE created < $cutoff",
            {"cutoff": cutoff},
        )
        or []
    )
    if not rows:
        return {"exported": 0, "deleted": 0, "file": None}
    os.makedirs(export_dir, exist_ok=True)
    filename = f"{table}-pre-prune.jsonl"
    path = os.path.join(export_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(row), default=str) + "\n")
    await repo_query(
        f"DELETE FROM {table} WHERE created < $cutoff",
        {"cutoff": cutoff},
    )
    return {"exported": len(rows), "deleted": len(rows), "file": filename}
