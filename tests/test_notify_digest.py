"""Due digest: logged always, pushed only when configured."""

import asyncio

import httpx

from open_notebook.utils import notify


def _fake_client(post):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return await post(*a, **k)

    return FakeClient


def test_digest_disabled_is_zero_footprint(monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_NOTIFY_URL", raising=False)

    class BoomClient:
        def __init__(self, *a, **k):
            raise AssertionError("httpx client must not be built when disabled")

    monkeypatch.setattr(notify.httpx, "AsyncClient", BoomClient)
    asyncio.run(notify.notify_digest("3 cards due"))
    assert notify._PENDING == set()


def test_digest_posts_text(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_NOTIFY_URL", "https://ntfy.example/topic")
    captured = {}

    async def post(url, json=None, headers=None):
        captured.update(url=url, json=json)
        return httpx.Response(200)

    monkeypatch.setattr(notify.httpx, "AsyncClient", _fake_client(post))
    asyncio.run(notify.notify_digest("3 cards due"))
    assert captured["url"] == "https://ntfy.example/topic"
    assert captured["json"]["message"] == "3 cards due"
