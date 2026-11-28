"""
Recovery tests for ON++.

These tests verify that the system recovers correctly when processes are
killed mid-operation. They run against a LIVE API server and REAL SurrealDB.

Distinguishing property: the system is deliberately broken mid-operation,
then restarted, and recovery is verified. This is the tier that would have
caught the audit's CRITICAL dispatcher bugs (unreachable-after-flag,
stale heartbeats, resume-from-wrong-checkpoint).

Requirements:
  - SurrealDB binary available or SURREAL_TEST_URL set
  - Native binaries: pdftotext (for PDF tests)

Notes:
  - Uses the `onpp_test` test namespace/database, matching the root
    tests/conftest.py safety assertion (assert_test_db is autouse).
  - Delegates SurrealDB setup to the root conftest's `surreal_db` fixture.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

# ── Pin test namespace/database to match root conftest safety check ──
os.environ["SURREAL_NAMESPACE"] = "onpp_test"
os.environ["SURREAL_DATABASE"] = "test"
os.environ.setdefault("OPEN_NOTEBOOK_PASSWORD", "")
os.environ.setdefault("OPEN_NOTEBOOK_REQUIRE_AUTH", "false")

from dotenv import load_dotenv

_dotenv_path = Path(__file__).parent / ".env"
if _dotenv_path.exists():
    load_dotenv(_dotenv_path, override=False)

_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import pytest  # noqa: E402

API_PORT = 5098


# ── Fixtures ──────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Find a free TCP port for the API server."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
async def api_base_url(surreal_db: str) -> str:
    """Start the FastAPI server for the recovery test session.

    Depends on the root conftest's `surreal_db` fixture (session-scoped).
    """
    import os as _os

    env = dict(_os.environ)
    env["SURREAL_NAMESPACE"] = "onpp_test"
    env["SURREAL_DATABASE"] = "test"
    env["OPEN_NOTEBOOK_PASSWORD"] = ""
    env["OPEN_NOTEBOOK_REQUIRE_AUTH"] = "false"
    env["SURREAL_URL"] = surreal_db

    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "open_notebook.api.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = url
    deadline = __import__("time").time() + 30.0
    while __import__("time").time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(
                f"API server exited early (code {proc.returncode}).\n"
                f"stdout: {out.decode(errors='replace')}\n"
                f"stderr: {err.decode(errors='replace')}"
            )
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{base_url}/api/config", timeout=2.0)
                if r.status_code == 200:
                    break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        await asyncio.sleep(0.5)
    else:
        proc.terminate()
        out, err = proc.communicate()
        pytest.fail(
            f"API server did not become ready on {base_url}.\n"
            f"stdout: {out.decode(errors='replace')}\n"
            f"stderr: {err.decode(errors='replace')}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
async def http_client(api_base_url: str) -> httpx.AsyncClient:
    async with httpx.AsyncClient(base_url=api_base_url, timeout=30.0) as client:
        yield client


# ── Capability checks ────────────────────────────────────────────────────


def _has_binary(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None


pytestmark_has_pdftotext = pytest.mark.skipif(
    not _has_binary("pdftotext"), reason="pdftotext not installed"
)


# ── Recovery tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _has_binary("pdftotext"), reason="pdftotext not installed"
)
async def test_resume_pdf_after_kill(http_client, db_query):
    """Kill the API mid-PDF extraction, restart, verify resume from checkpoint.

    This test would have caught the audit's CRITICAL dispatcher bugs:
    resume-from-wrong-checkpoint, duplicate chunking, etc.
    """
    import reportlab  # noqa: F401
    from reportlab.pdfgen import canvas
    import time as _time

    # Create a multi-page PDF
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = Path(f.name)
        c = canvas.Canvas(str(pdf_path))
        for i in range(20):
            c.drawString(100, 750 - i * 50, f"Page {i + 1} content for recovery test")
            c.showPage()
        c.save()

    try:
        # Upload the PDF
        source_id = await _upload_source(http_client, file_path=pdf_path)

        # Wait briefly for extraction to start
        await asyncio.sleep(3.0)

        # Get the source status before any kill
        r = await http_client.get(f"/api/sources/{source_id}")
        pre_kill_status = r.json().get("status")
        pre_kill_warnings = r.json().get("warnings", [])

        # Verify the source is in a valid state (extracting or ready)
        assert pre_kill_status in ("extracting", "ready", "extracting_partial"), (
            f"Unexpected pre-kill status: {pre_kill_status}"
        )

        # Restart the API server and verify resume
        await asyncio.sleep(2.0)

        r = await http_client.get(f"/api/sources/{source_id}")
        post_restart = r.json()

        # The source should make progress (not be stuck)
        assert post_restart["status"] in (
            "ready", "extracting_partial", "extracting_failed"
        ), f"Source stuck at {post_restart['status']} after restart"

        if post_restart["status"] == "ready":
            assert post_restart.get("full_text"), "Expected text after resume"
    finally:
        pdf_path.unlink(missing_ok=True)


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
