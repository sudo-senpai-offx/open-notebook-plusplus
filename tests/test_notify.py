"""D1 ntfy notification tests (httpx mocked, no network)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from open_notebook.utils import notify

ENV_VARS = (
    "OPEN_NOTEBOOK_NOTIFY_URL",
    "OPEN_NOTEBOOK_NOTIFY_TOKEN",
    "OPEN_NOTEBOOK_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    notify._PENDING.clear()


def _fake_client(post_impl):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return await post_impl(*a, **k)

    return FakeClient


def test_disabled_is_zero_footprint(monkeypatch):
    """Unset URL => no DB read, no httpx client, no task."""

    async def boom(*a, **k):
        raise AssertionError("repo_query must not run when notify is disabled")

    monkeypatch.setattr(notify, "repo_query", boom)

    class BoomClient:
        def __init__(self, *a, **k):
            raise AssertionError("httpx client must not be built when disabled")

    monkeypatch.setattr(notify.httpx, "AsyncClient", BoomClient)

    asyncio.run(
        notify.notify_job_event(
            "completed",
            {"id": "command:1", "name": "process_source", "status": "completed"},
        )
    )
    assert notify._PENDING == set()


def test_posts_payload_with_token_and_click(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_NOTIFY_URL", "https://ntfy.example/topic")
    monkeypatch.setenv("OPEN_NOTEBOOK_NOTIFY_TOKEN", "tok")
    monkeypatch.setenv("OPEN_NOTEBOOK_BASE_URL", "http://box:8502/")
    captured: dict = {}

    async def post(url, json=None, headers=None):
        captured.update(url=url, json=json, headers=headers)
        return httpx.Response(200)

    monkeypatch.setattr(notify.httpx, "AsyncClient", _fake_client(post))
    monkeypatch.setattr(
        notify,
        "_resolve_subject",
        AsyncMock(return_value={"name": "My Source", "path": "/sources/source%3Aabc"}),
    )

    async def run():
        await notify.notify_job_event(
            "completed",
            {"id": "command:1", "name": "embed_source", "status": "completed"},
        )
        await asyncio.gather(*notify._PENDING)

    asyncio.run(run())

    assert captured["url"] == "https://ntfy.example/topic"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["json"]["title"] == "✓ embed source complete"
    assert captured["json"]["priority"] == 3
    assert captured["json"]["message"] == "My Source"
    assert captured["json"]["click"] == "http://box:8502/sources/source%3Aabc"


def test_send_failure_is_swallowed(monkeypatch):
    """GREEN case: server error -> one warning, never raises, no retry."""
    monkeypatch.setenv("OPEN_NOTEBOOK_NOTIFY_URL", "https://ntfy.example/topic")
    fake_logger = MagicMock()
    monkeypatch.setattr(notify, "logger", fake_logger)

    async def post(*a, **k):
        return httpx.Response(500)

    monkeypatch.setattr(notify.httpx, "AsyncClient", _fake_client(post))
    monkeypatch.setattr(notify, "_resolve_subject", AsyncMock(return_value=None))

    async def run():
        await notify.notify_job_event(
            "completed",
            {"id": "command:1", "name": "process_source", "status": "completed"},
        )
        await asyncio.gather(*notify._PENDING)

    asyncio.run(run())  # must not raise
    assert fake_logger.warning.call_count == 1


def test_unreachable_url_is_swallowed(monkeypatch):
    """GREEN case: connection failure -> one warning, never raises."""
    monkeypatch.setenv("OPEN_NOTEBOOK_NOTIFY_URL", "http://10.255.255.1:9/topic")
    fake_logger = MagicMock()
    monkeypatch.setattr(notify, "logger", fake_logger)

    async def post(*a, **k):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(notify.httpx, "AsyncClient", _fake_client(post))
    monkeypatch.setattr(notify, "_resolve_subject", AsyncMock(return_value=None))

    async def run():
        await notify.notify_job_event(
            "failed",
            {
                "id": "command:2",
                "name": "process_source",
                "status": "failed",
                "error_message": "boom",
            },
        )
        await asyncio.gather(*notify._PENDING)

    asyncio.run(run())
    assert fake_logger.warning.call_count == 1


def test_resolve_subject_source_notebook_and_fallback(monkeypatch):
    async def source_query(q, vars=None):
        return [{"title": "My Source"}]

    monkeypatch.setattr(notify, "repo_query", source_query)
    s = asyncio.run(notify._resolve_subject({"source_id": "source:abc"}))
    assert s == {"name": "My Source", "path": "/sources/source%3Aabc"}

    async def notebook_query(q, vars=None):
        return [{"name": "My Notebook"}]

    monkeypatch.setattr(notify, "repo_query", notebook_query)
    n = asyncio.run(notify._resolve_subject({"notebook_ids": ["notebook:xyz"]}))
    assert n == {"name": "My Notebook", "path": "/notebooks/notebook%3Axyz"}

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(notify, "repo_query", boom)
    f = asyncio.run(notify._resolve_subject({"source_id": "source:abc"}))
    assert f == {"name": "source:abc", "path": "/sources/source%3Aabc"}

    assert asyncio.run(notify._resolve_subject({})) is None


def test_build_payload_failed_includes_reason(monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_BASE_URL", raising=False)
    p = notify._build_payload(
        "failed",
        {"name": "process_source", "error_message": "stale: no heartbeat"},
        None,
    )
    assert p["title"] == "✗ process source failed"
    assert p["priority"] == 4
    assert "stale: no heartbeat" in p["message"]
    assert "click" not in p
