"""Study metrics derived per request from event logs (P4).

No stored counters anywhere: revlog rows + focus_session rows + the due
query are the sources. Histories are small on this floor; precompute only
when measurement says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional


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


def _day(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def compute_summary(
    *,
    reviews: List[Dict[str, Any]],
    sessions: List[Dict[str, Any]],
    due_count: int,
    now: datetime,
) -> Dict[str, Any]:
    today = _day(now)
    week_ago = today - timedelta(days=7)

    today_count = 0
    lapses = 0
    durations: List[int] = []
    review_days = set()
    for r in reviews:
        ts = _parse_ts(r.get("reviewed_at"))
        if ts is None:
            continue
        day = _day(ts)
        review_days.add(day)
        if day == today:
            today_count += 1
        if ts >= week_ago and str(r.get("rating")) == "again":
            lapses += 1
        try:
            durations.append(int(r.get("duration_ms", 0)))
        except (TypeError, ValueError):
            pass

    streak = 0
    cursor = today if today in review_days else today - timedelta(days=1)
    while cursor in review_days:
        streak += 1
        cursor -= timedelta(days=1)

    focus_minutes = 0
    for s in sessions:
        start = _parse_ts(s.get("started_at"))
        end = _parse_ts(s.get("ended_at"))
        if start is None or end is None or start < week_ago:
            continue
        focus_minutes += max(0, int((end - start).total_seconds() // 60))

    return {
        "due_count": int(due_count),
        "reviews_today": today_count,
        "lapses_7d": lapses,
        "median_ms": int(median(durations)) if durations else None,
        "streak_days": streak,
        "focus_minutes_7d": focus_minutes,
    }


async def build_summary() -> Dict[str, Any]:
    """Fetch event windows and derive the summary."""
    from open_notebook.database.repository import repo_query
    from open_notebook.review import store

    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=370)).isoformat()
    reviews = await repo_query(
        "SELECT rating, reviewed_at, duration_ms FROM revlog "
        "WHERE reviewed_at >= $since ORDER BY reviewed_at DESC LIMIT 5000",
        {"since": since},
    )
    sessions = await repo_query(
        "SELECT started_at, ended_at FROM focus_session "
        "WHERE status != 'running' AND started_at >= $since",
        {"since": since},
    )
    due = await store.list_due_cards(limit=1000)
    return compute_summary(
        reviews=list(rows_or_empty(reviews)),
        sessions=list(rows_or_empty(sessions)),
        due_count=len(due),
        now=now,
    )


def rows_or_empty(rows: Any) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows] if rows else []
