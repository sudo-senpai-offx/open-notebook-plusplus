"""Digest edge: one ntfy per UTC day when cards are due."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from api import governor as governor_module
from api.governor import Governor

NOW = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)


def _enable(monkeypatch, value=True):
    if value:
        monkeypatch.setenv("OPEN_NOTEBOOK_HIBERNATE_ENABLE", "true")
    else:
        monkeypatch.delenv("OPEN_NOTEBOOK_HIBERNATE_ENABLE", raising=False)


@pytest.mark.asyncio
async def test_digest_fires_once_per_day(monkeypatch):
    async def fake_query(*args, **kwargs):
        return []

    monkeypatch.setattr(governor_module, "repo_query", fake_query)
    _enable(monkeypatch, False)

    async def fake_due(limit=1000):
        return [{"id": "flashcard:1"}]

    monkeypatch.setattr("open_notebook.review.store.list_due_cards", fake_due)
    with patch("api.governor.notify_digest", new=AsyncMock()) as digest:
        gov = Governor()
        await gov.run_once(now=NOW)
        await gov.run_once(now=NOW + timedelta(minutes=5))
    assert digest.await_count == 1
    assert digest.await_args is not None
    assert "1" in digest.await_args[0][0]


@pytest.mark.asyncio
async def test_no_digest_when_nothing_due(monkeypatch):
    async def fake_query(*args, **kwargs):
        return []

    monkeypatch.setattr(governor_module, "repo_query", fake_query)
    _enable(monkeypatch, False)
    monkeypatch.setattr(
        "open_notebook.review.store.list_due_cards", AsyncMock(return_value=[])
    )
    with patch("api.governor.notify_digest", new=AsyncMock()) as digest:
        await Governor().run_once(now=NOW)
    assert digest.await_count == 0


@pytest.mark.asyncio
async def test_digest_fires_next_day(monkeypatch):
    async def fake_query(*args, **kwargs):
        return []

    monkeypatch.setattr(governor_module, "repo_query", fake_query)
    _enable(monkeypatch, False)

    async def fake_due(limit=1000):
        return [{"id": "flashcard:1"}]

    monkeypatch.setattr("open_notebook.review.store.list_due_cards", fake_due)
    with patch("api.governor.notify_digest", new=AsyncMock()) as digest:
        gov = Governor()
        await gov.run_once(now=NOW)
        await gov.run_once(now=NOW + timedelta(days=1))
    assert digest.await_count == 2
