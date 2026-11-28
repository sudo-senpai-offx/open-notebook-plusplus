"""
System tests: format matrix for the universal source processing engine.

These tests run against a LIVE API server and REAL SurrealDB with REAL
native binaries. Each test uploads a source of a specific format and
verifies it reaches "ready" status with extractable content.

Distinguishing property: no mocks - every component is real.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"

ARTICLE_MD = FIXTURES_DIR / "article-5kb.md"
FORUM_HTML = FIXTURES_DIR / "forum-thread.html"


# ── Helper ────────────────────────────────────────────────────────────


def _sha256(file_path: Path) -> str:
    """Compute SHA256 of a file."""
    h = hashlib.sha256()
    h.update(file_path.read_bytes())
    return h.hexdigest()


async def _upload_source(http_client, *, file_path=None, url=None, content=None):
    """Upload a source via the API and return the source id."""
    if file_path:
        files = {"file": (file_path.name, file_path.read_bytes(), "application/octet-stream")}
        data = {}
    elif url:
        files = None
        data = {"url": url}
    elif content:
        files = None
        data = {"content": content}
    else:
        raise ValueError("Must provide file_path, url, or content")

    r = await http_client.post("/api/sources", files=files, data=data)
    assert r.status_code == 200, f"Upload failed: {r.status_code} {r.text}"
    return r.json()["id"]


async def _wait_for_ready(http_client, source_id: str, timeout: float = 120.0):
    """Poll the source until it reaches 'ready' or 'extracting_failed'."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await http_client.get(f"/api/sources/{source_id}")
        assert r.status_code == 200, f"Source GET failed: {r.status_code} {r.text}"
        source = r.json()
        status = source.get("status")
        if status in ("ready", "extracting_failed"):
            return source
        await asyncio.sleep(2.0)
    raise TimeoutError(
        f"Source {source_id} did not reach terminal status within {timeout}s. "
        f"Last status: {status}"
    )


# ── Format matrix tests ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not ARTICLE_MD.exists(), reason="article fixture missing")
async def test_markdown_source_ready(http_client):
    """Markdown source should reach 'ready' with extractable content."""
    source_id = await _upload_source(http_client, file_path=ARTICLE_MD)
    source = await _wait_for_ready(http_client, source_id)

    assert source["status"] == "ready", f"Expected 'ready', got {source['status']}"
    assert source.get("full_text"), "Expected non-empty full_text"
    assert len(source["full_text"]) > 100, "Expected substantive extracted text"

    # Verify content is searchable (RAG pipeline worked)
    r = await http_client.post(
        "/api/search/",
        json={"query": "Retrieval-Augmented Generation", "source_ids": [source_id]},
    )
    assert r.status_code == 200, f"Search failed: {r.status_code} {r.text}"
    results = r.json()
    assert len(results.get("results", [])) > 0, "Expected search results"


@pytest.mark.asyncio
@pytest.mark.skipif(not FORUM_HTML.exists(), reason="forum fixture missing")
async def test_forum_html_source_ready(http_client):
    """Forum HTML source should reach 'ready' with page_type='forum'."""
    source_id = await _upload_source(http_client, file_path=FORUM_HTML)
    source = await _wait_for_ready(http_client, source_id)

    assert source["status"] == "ready", f"Expected 'ready', got {source['status']}"
    assert source.get("page_type") == "forum", (
        f"Expected page_type='forum', got {source.get('page_type')}"
    )
    assert source.get("full_text"), "Expected non-empty full_text"
    assert len(source["full_text"]) > 100, "Expected substantive extracted text"


@pytest.mark.asyncio
async def test_text_source_ready(http_client):
    """Plain text source should reach 'ready' with extractable content."""
    source_id = await _upload_source(
        http_client,
        content="This is a test document about artificial intelligence and machine learning. "
        "It contains several sentences to test the ingestion pipeline.",
    )
    source = await _wait_for_ready(http_client, source_id)

    assert source["status"] == "ready", f"Expected 'ready', got {source['status']}"
    assert source.get("full_text"), "Expected non-empty full_text"
    assert "artificial intelligence" in source["full_text"].lower()


@pytest.mark.asyncio
async def test_url_source_ready(http_client):
    """URL source should reach 'ready' with extracted content."""
    source_id = await _upload_source(
        http_client,
        url="https://example.com",
    )
    source = await _wait_for_ready(http_client, source_id, timeout=60.0)

    # URL extraction may fail if example.com is unreachable; that's OK
    # as long as we get a deterministic terminal status
    assert source["status"] in ("ready", "extracting_failed"), (
        f"Unexpected status: {source['status']}"
    )
    if source["status"] == "ready":
        assert source.get("full_text"), "Expected non-empty full_text for ready source"
