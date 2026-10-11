"""Review endpoints against the store seam (mocked; storage next slice)."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client():
    from api.main import app

    return TestClient(app)


def test_due_lists_cards():
    cards = [{"id": "flashcard:1", "model": "basic", "front": "Q?"}]
    with patch(
        "api.routers.review.store.list_due_cards",
        new=AsyncMock(return_value=cards),
    ):
        response = _client().get("/api/review/due")
    assert response.status_code == 200
    assert response.json() == cards


def test_grade_records_and_returns_due():
    with patch(
        "api.routers.review.store.record_grade",
        new=AsyncMock(return_value={"id": "flashcard:1", "due": "tomorrow"}),
    ):
        response = _client().post(
            "/api/review/grade/flashcard:1", json={"rating": "good"}
        )
    assert response.status_code == 200
    assert response.json()["id"] == "flashcard:1"


def test_grade_rejects_bad_rating():
    response = _client().post(
        "/api/review/grade/flashcard:1", json={"rating": "brilliant"}
    )
    assert response.status_code == 422


def test_grade_surfaces_store_denial():
    with patch(
        "api.routers.review.store.record_grade",
        new=AsyncMock(side_effect=ValueError("only approved")),
    ):
        response = _client().post(
            "/api/review/grade/flashcard:1", json={"rating": "good"}
        )
    assert response.status_code == 400


def test_retention_target_is_locked():
    response = _client().get("/api/review/retention")
    assert response.json() == {"desired_retention": 0.85}
