"""Store wiring: grading persists FSRS state and appends a revlog row."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.review import store


@pytest.mark.asyncio
async def test_record_grade_saves_state_and_logs():
    card = SimpleNamespace(status="approved", fsrs_json=None, save=AsyncMock())
    queries = []

    async def fake_query(q, vars=None):
        queries.append((q, vars or {}))
        return []

    async def fake_get(_id):
        return card

    with (
        patch("open_notebook.review.store.repo_query", new=fake_query),
        patch(
            "open_notebook.domain.flashcard.Flashcard.get",
            new=fake_get,
        ),
    ):
        result = await store.record_grade("flashcard:1", "good", duration_ms=4200)
    assert result["id"] == "flashcard:1"
    assert "due" in result
    card.save.assert_awaited_once()
    assert any("CREATE revlog" in q for q, _ in queries)
    assert any(v.get("duration_ms") == 4200 for _, v in queries)
