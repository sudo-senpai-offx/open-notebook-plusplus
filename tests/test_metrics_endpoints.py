"""Metrics endpoint over the summary builder (mocked here)."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client():
    from api.main import app

    return TestClient(app)


def test_summary_shape():
    summary = {
        "due_count": 3,
        "reviews_today": 5,
        "lapses_7d": 1,
        "median_ms": 4000,
        "streak_days": 2,
        "focus_minutes_7d": 50,
    }
    with patch(
        "api.routers.metrics.build_summary", new=AsyncMock(return_value=summary)
    ):
        response = _client().get("/api/metrics/summary")
    assert response.status_code == 200
    assert response.json() == summary


def test_summary_failure_is_500():
    with patch(
        "api.routers.metrics.build_summary",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        response = _client().get("/api/metrics/summary")
    assert response.status_code == 500
