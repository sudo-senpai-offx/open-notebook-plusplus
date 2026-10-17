"""`/processing/status` must not report a dead database as an idle queue.

The endpoint caught every database failure and substituted `queue = {}`. On the
phone, with SurrealDB not listening at all, that produced:

    {"queue": {"completed": 0, "held": 0, "failed": 0}, ...}

which reads as "healthy, nothing queued" on the API and in the UI. The only
evidence anything was wrong was a log line nobody was watching. An operator
asking whether the queue was draining was told there was no queue, when the
truth was that nobody could ask.

Same shape as the other silent failures this session kept producing: a degraded
component reporting the healthy-looking value of a *different* question.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.processing import router


@pytest.fixture
def api(monkeypatch):
    import api.routers.processing as mod

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app), mod


def _patch_status_queries(mod, monkeypatch, fail: bool) -> None:
    """Make every repo_query in the status path behave as asked."""

    async def query(*args: Any, **kwargs: Any):
        if fail:
            raise ConnectionRefusedError("Connect call failed ('127.0.0.1', 8000)")
        return []

    monkeypatch.setattr(mod, "repo_query", query)

    # Imported *inside* the handler, so patching the router namespace would
    # not reach it - the same trap the rebuild-status test hit.
    import open_notebook.sync.extraction as extraction

    async def no_retry(*args: Any, **kwargs: Any):
        return []

    monkeypatch.setattr(extraction, "_skip_reason", no_retry)


class TestDeadDatabaseIsNotAnEmptyQueue:
    def test_flags_itself_unavailable(self, api, monkeypatch):
        client, mod = api
        _patch_status_queries(mod, monkeypatch, fail=True)

        body = client.get("/api/processing/status").json()

        assert body["queue_available"] is False, (
            "a caller cannot distinguish 'nothing queued' from 'could not "
            "look' unless the endpoint says which one it is"
        )

    def test_still_returns_the_shape(self, api, monkeypatch):
        """Never breaking callers was the right call; the flag is what makes
        that safe rather than misleading."""
        client, mod = api
        _patch_status_queries(mod, monkeypatch, fail=True)

        response = client.get("/api/processing/status")

        assert response.status_code == 200
        assert "queue" in response.json()

    def test_a_healthy_device_is_available(self, api, monkeypatch):
        client, mod = api
        _patch_status_queries(mod, monkeypatch, fail=False)

        body = client.get("/api/processing/status").json()

        assert body["queue_available"] is True

    def test_an_empty_but_reachable_queue_is_available(self, api, monkeypatch):
        """The distinction that matters: zero real rows is not the same as no
        answer. Only the second one is a fault."""
        client, mod = api
        _patch_status_queries(mod, monkeypatch, fail=False)

        body = client.get("/api/processing/status").json()

        assert body["queue"] == {}
        assert body["queue_available"] is True, (
            "an idle queue is a real measurement and must not be reported as a failure"
        )


class TestFieldIsAlwaysPresent:
    @pytest.mark.parametrize("fail", [True, False])
    def test_present_either_way(self, api, monkeypatch, fail: bool):
        """A flag that only appears on failure cannot be relied on by a
        caller checking `if body.get('queue_available')`."""
        client, mod = api
        _patch_status_queries(mod, monkeypatch, fail=fail)

        assert "queue_available" in client.get("/api/processing/status").json()
