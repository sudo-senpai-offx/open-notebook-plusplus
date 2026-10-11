"""Wake/state endpoints: authed, backed by the lifespan governor."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


def test_wake_without_governor_is_503():
    from api.main import app

    if hasattr(app.state, "governor"):
        delattr(app.state, "governor")
    response = TestClient(app).post("/api/governor/wake")
    assert response.status_code == 503


def test_wake_delegates_and_reports():
    from api.main import app

    governor = SimpleNamespace(wake=AsyncMock(), _asleep=True)
    app.state.governor = governor
    try:
        response = TestClient(app).post("/api/governor/wake")
        assert response.status_code == 200
        assert response.json() == {"woke": True}
        governor.wake.assert_awaited_once()
    finally:
        delattr(app.state, "governor")


def test_wake_failure_is_502():
    from api.main import app

    async def _boom():
        raise RuntimeError("supervisor unreachable")

    governor = SimpleNamespace(wake=_boom, _asleep=True)
    app.state.governor = governor
    try:
        response = TestClient(app).post("/api/governor/wake")
        assert response.status_code == 502
    finally:
        delattr(app.state, "governor")


def test_state_reflects_asleep():
    from api.main import app

    app.state.governor = SimpleNamespace(_asleep=False)
    try:
        assert TestClient(app).get("/api/governor/state").json() == {"asleep": False}
    finally:
        delattr(app.state, "governor")
