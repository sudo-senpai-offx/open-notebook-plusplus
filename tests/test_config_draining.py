"""T3: GET /api/config exposes the live draining signal.

Fail-open: when the service-state record is unreadable, `draining` is False
(DB-down is already carried by `dbStatus`). /health stays trivial.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


def _ok_db():
    return patch(
        "api.routers.config.repo_query",
        new=AsyncMock(return_value=[{"result": 1}]),
    )


def test_draining_true_when_record_set(client):
    with (
        _ok_db(),
        patch(
            "api.routers.config.ServiceState.get_instance",
            new=AsyncMock(return_value=SimpleNamespace(draining=True)),
        ),
    ):
        body = client.get("/api/config").json()
        assert body["draining"] is True


def test_draining_false_by_default(client):
    with (
        _ok_db(),
        patch(
            "api.routers.config.ServiceState.get_instance",
            new=AsyncMock(return_value=SimpleNamespace(draining=False)),
        ),
    ):
        body = client.get("/api/config").json()
        assert body["draining"] is False


def test_draining_fail_open_when_record_unreadable(client):
    with (
        _ok_db(),
        patch(
            "api.routers.config.ServiceState.get_instance",
            new=AsyncMock(side_effect=RuntimeError("db gone")),
        ),
    ):
        body = client.get("/api/config").json()
        assert body["draining"] is False
        assert body["dbStatus"] == "online"
