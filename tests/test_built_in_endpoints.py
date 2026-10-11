"""P2 endpoints: list is pure data; install delegates to the normal save."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client():
    from api.main import app

    return TestClient(app)


def test_list_built_in_needs_no_database():
    response = _client().get("/api/transformations/built-in")
    assert response.status_code == 200
    keys = [b["key"] for b in response.json()]
    assert keys == ["cloze", "basic-qa", "mcq-check"]


def test_install_unknown_is_404():
    response = _client().post("/api/transformations/built-in/nope")
    assert response.status_code == 404


def test_install_duplicate_is_409():
    existing = SimpleNamespace(name="flashcard_cloze")
    with patch(
        "api.routers.transformations.Transformation.get_all",
        new=AsyncMock(return_value=[existing]),
    ):
        response = _client().post("/api/transformations/built-in/cloze")
    assert response.status_code == 409


def test_install_saves_and_returns():
    saved = {}

    async def fake_save(self):
        saved["prompt"] = self.prompt

    with (
        patch(
            "api.routers.transformations.Transformation.get_all",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "api.routers.transformations.Transformation.save",
            new=fake_save,
        ),
    ):
        response = _client().post("/api/transformations/built-in/basic-qa")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "flashcard_qa"
    assert "ONE FACT" in saved["prompt"]
