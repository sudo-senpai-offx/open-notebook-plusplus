"""Retention prune: export-then-delete, commands table never touched."""

import json
from unittest.mock import patch

import pytest

from open_notebook.maintenance import prune as prune_module
from open_notebook.maintenance.prune import export_and_prune, retention_days


def test_retention_days_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_LOG_RETENTION_DAYS", raising=False)
    assert retention_days() == 365
    monkeypatch.setenv("OPEN_NOTEBOOK_LOG_RETENTION_DAYS", "junk")
    assert retention_days() == 365
    monkeypatch.setenv("OPEN_NOTEBOOK_LOG_RETENTION_DAYS", "5")
    assert retention_days() == 30


@pytest.mark.asyncio
async def test_export_then_delete_order(tmp_path):
    calls = []

    async def fake_query(q, vars=None):
        calls.append(q)
        if q.startswith("SELECT"):
            return [{"id": "revlog:1", "rating": "good"}]
        return []

    with patch.object(prune_module, "repo_query", new=fake_query):
        result = await export_and_prune(
            "revlog", "2020-01-01T00:00:00+00:00", str(tmp_path)
        )
    assert result["exported"] == 1
    assert result["deleted"] == 1
    assert calls[0].startswith("SELECT")
    assert "DELETE" in calls[1]
    exported = json.loads((tmp_path / result["file"]).read_text().splitlines()[0])
    assert exported["rating"] == "good"


@pytest.mark.asyncio
async def test_empty_table_no_file_no_delete(tmp_path):
    calls = []

    async def fake_query(q, vars=None):
        calls.append(q)
        return []

    with patch.object(prune_module, "repo_query", new=fake_query):
        result = await export_and_prune(
            "revlog", "2020-01-01T00:00:00+00:00", str(tmp_path)
        )
    assert result == {"exported": 0, "deleted": 0, "file": None}
    assert len(calls) == 1
