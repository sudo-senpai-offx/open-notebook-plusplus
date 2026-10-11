"""T3: observe-only governor decides but never acts.

Idle is DB-visible only: no command created/active within the idle window
and no blocking leases. Disabled flag means dormant, always.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.governor import evaluate

NOW = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)


def _row(status, created_ago_s, hb_ago_s=None, name="process_source"):
    row = {
        "id": "command:test",
        "name": name,
        "status": status,
        "created": (NOW - timedelta(seconds=created_ago_s)).isoformat(),
    }
    hb_ago = hb_ago_s if hb_ago_s is not None else created_ago_s
    row["heartbeat_at"] = (NOW - timedelta(seconds=hb_ago)).isoformat()
    return row


def test_disabled_is_dormant():
    decision = evaluate(
        enabled=False, idle_seconds=600, rows=[_row("running", 10)], now=NOW
    )
    assert decision["action"] == "dormant"


def test_blocking_lease_vetoes():
    decision = evaluate(
        enabled=True, idle_seconds=600, rows=[_row("running", 10)], now=NOW
    )
    assert decision["action"] == "vetoed"


def test_queued_job_vetoes():
    decision = evaluate(
        enabled=True, idle_seconds=600, rows=[_row("new", 700)], now=NOW
    )
    assert decision["action"] == "vetoed"


def test_recently_completed_counts_as_activity():
    rows = [_row("completed", 700, hb_ago_s=100)]
    decision = evaluate(enabled=True, idle_seconds=600, rows=rows, now=NOW)
    assert decision["action"] == "active"


def test_idle_table_would_hibernate():
    rows = [_row("completed", 3600, hb_ago_s=3600)]
    decision = evaluate(enabled=True, idle_seconds=600, rows=rows, now=NOW)
    assert decision["action"] == "would_hibernate"


def test_never_used_table_would_hibernate():
    decision = evaluate(enabled=True, idle_seconds=600, rows=[], now=NOW)
    assert decision["action"] == "would_hibernate"


def test_stale_running_does_not_veto_but_counts_activity():
    # Heartbeat 400s ago: stale at 300s threshold -> no veto; but the
    # heartbeat itself is recent activity (< 600s idle window) -> active.
    rows = [_row("running", 400, hb_ago_s=400)]
    decision = evaluate(enabled=True, idle_seconds=600, rows=rows, now=NOW)
    assert decision["action"] == "active"
