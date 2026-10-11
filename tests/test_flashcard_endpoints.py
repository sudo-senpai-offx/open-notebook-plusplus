"""P3 inbox endpoints: drafts in, approvals out, tombstones for the rest."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client():
    from api.main import app

    return TestClient(app)


def _card(**over):
    base = {
        "id": "flashcard:t1",
        "source_id": "source:s1",
        "model": "basic",
        "front": "Q?",
        "back": "A.",
        "cloze_text": None,
        "tags": [],
        "guid": None,
        "status": "draft",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_inbox_lists_drafts():
    rows = [
        {"id": "flashcard:a", "status": "draft", "model": "basic", "front": "Q?"}
    ]
    with patch(
        "api.routers.flashcards.repo_query", new=AsyncMock(return_value=rows)
    ):
        response = _client().get("/api/flashcards?status=draft")
    assert response.status_code == 200
    assert response.json()[0]["id"] == "flashcard:a"


def test_approve_stamps_guid_and_flips():
    card = _card()
    card.save = AsyncMock()

    async def fake_get(_id):
        from open_notebook.domain.flashcard import Flashcard

        assert isinstance(card, Flashcard) or True
        return card

    with (
        patch("api.routers.flashcards.Flashcard.get", new=AsyncMock(return_value=card)),
        patch("api.routers.flashcards.content_guid", return_value="g" * 16),
    ):
        response = _client().post("/api/flashcards/flashcard:t1/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["guid"] == "g" * 16


def test_approve_non_draft_conflicts():
    card = _card(status="approved")
    with patch("api.routers.flashcards.Flashcard.get", new=AsyncMock(return_value=card)):
        response = _client().post("/api/flashcards/flashcard:t1/approve")
    assert response.status_code == 409


def test_edit_draft_fields():
    card = _card()
    card.save = AsyncMock()
    with patch("api.routers.flashcards.Flashcard.get", new=AsyncMock(return_value=card)):
        response = _client().put(
            "/api/flashcards/flashcard:t1", json={"front": "Better Q?"}
        )
    assert response.status_code == 200
    assert response.json()["front"] == "Better Q?"


def test_edit_approved_conflicts():
    card = _card(status="approved")
    with patch("api.routers.flashcards.Flashcard.get", new=AsyncMock(return_value=card)):
        response = _client().put(
            "/api/flashcards/flashcard:t1", json={"front": "X"}
        )
    assert response.status_code == 409


def test_reject_hard_deletes_draft():
    card = _card()
    card.delete = AsyncMock(return_value=True)
    with patch("api.routers.flashcards.Flashcard.get", new=AsyncMock(return_value=card)):
        response = _client().post("/api/flashcards/flashcard:t1/reject")
    assert response.status_code == 200
    assert response.json()["deleted"] is True


def test_reject_approved_points_to_tombstone():
    card = _card(status="approved")
    with patch("api.routers.flashcards.Flashcard.get", new=AsyncMock(return_value=card)):
        response = _client().post("/api/flashcards/flashcard:t1/reject")
    assert response.status_code == 409
    assert "tombstone" in response.json()["detail"].lower()


def test_tombstone_approved():
    card = _card(status="approved")
    card.save = AsyncMock()
    with patch("api.routers.flashcards.Flashcard.get", new=AsyncMock(return_value=card)):
        response = _client().post("/api/flashcards/flashcard:t1/tombstone")
    assert response.status_code == 200
    assert response.json()["status"] == "tombstoned"
