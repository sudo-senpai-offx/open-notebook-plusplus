"""Focus lease endpoints: start registers, end closes, open lists."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from open_notebook.exceptions import NotFoundError


def _client():
    from api.main import app

    return TestClient(app)


def test_start_registers_running_lease():
    saved = {}

    async def fake_save(self):
        saved["status"] = self.status
        self.id = "focus_session:test1"

    with patch("api.routers.focus.FocusSession.save", new=fake_save):
        response = _client().post(
            "/api/focus/sessions",
            json={"preset": "pomodoro", "planned_seconds": 1500},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["preset"] == "pomodoro"
    assert saved["status"] == "running"


def test_start_rejects_short_plan():
    response = _client().post("/api/focus/sessions", json={"planned_seconds": 10})
    assert response.status_code == 422


def test_end_closes_running_session():
    session = SimpleNamespace(
        id="focus_session:test1",
        notebook_id=None,
        preset="pomodoro",
        planned_seconds=1500,
        status="running",
        ended_at=None,
        save=AsyncMock(),
    )
    with patch(
        "api.routers.focus.FocusSession.get", new=AsyncMock(return_value=session)
    ):
        response = _client().post(
            "/api/focus/sessions/focus_session:test1/end", json={"status": "done"}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    session.save.assert_awaited_once()


def test_end_missing_is_404():
    with patch(
        "api.routers.focus.FocusSession.get",
        new=AsyncMock(side_effect=NotFoundError("gone")),
    ):
        response = _client().post(
            "/api/focus/sessions/focus_session:nope/end", json={"status": "done"}
        )
    assert response.status_code == 404


def test_end_finished_is_409():
    session = SimpleNamespace(status="done")
    with patch(
        "api.routers.focus.FocusSession.get", new=AsyncMock(return_value=session)
    ):
        response = _client().post(
            "/api/focus/sessions/focus_session:test1/end", json={"status": "done"}
        )
    assert response.status_code == 409


def test_open_lists_running():
    rows = [
        {
            "id": "focus_session:a",
            "notebook_id": None,
            "preset": "pomodoro",
            "planned_seconds": 1500,
            "status": "running",
        }
    ]
    with patch("api.routers.focus.repo_query", new=AsyncMock(return_value=rows)):
        response = _client().get("/api/focus/sessions/open")
    assert response.status_code == 200
    assert response.json()[0]["id"] == "focus_session:a"
