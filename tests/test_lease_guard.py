"""T3: the lease guard vetoes hibernate while work is leased.

Deny on queued (`new`) or fresh-heartbeat `running` rows; a stale `running`
row must NOT veto (its worker is gone — JobMonitor will mark it failed).
Empty table allows hibernate. Pure core is tested without a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from open_notebook.utils import lease_guard
from open_notebook.utils.lease_guard import blocking_leases, hibernate_allowed

NOW = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)


def _row(status, heartbeat=None):
    row = {"id": "command:test", "name": "process_source", "status": status}
    if heartbeat is not None:
        row["heartbeat_at"] = heartbeat.isoformat()
    return row


def test_fresh_running_blocks():
    rows = [_row("running", NOW - timedelta(seconds=30))]
    assert len(blocking_leases(rows, NOW, 300)) == 1


def test_queued_new_blocks_without_heartbeat():
    rows = [_row("new")]
    assert len(blocking_leases(rows, NOW, 300)) == 1


def test_stale_running_does_not_block():
    rows = [_row("running", NOW - timedelta(seconds=601))]
    assert blocking_leases(rows, NOW, 300) == []


def test_terminal_and_empty_allow():
    assert blocking_leases([], NOW, 300) == []
    rows = [_row("completed"), _row("failed"), _row("canceled")]
    assert blocking_leases(rows, NOW, 300) == []


@pytest.mark.asyncio
async def test_hibernate_allowed_denies_on_active(monkeypatch):
    async def fake_query(*args, **kwargs):
        return [_row("running", NOW - timedelta(seconds=10))]

    monkeypatch.setattr(lease_guard, "repo_query", fake_query)
    allowed, reason = await hibernate_allowed(now=NOW)
    assert allowed is False
    assert "process_source" in reason


@pytest.mark.asyncio
async def test_hibernate_allowed_permits_empty(monkeypatch):
    async def fake_query(*args, **kwargs):
        return []

    monkeypatch.setattr(lease_guard, "repo_query", fake_query)
    allowed, _ = await hibernate_allowed(now=NOW)
    assert allowed is True
