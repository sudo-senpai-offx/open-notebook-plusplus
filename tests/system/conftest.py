"""
System-level integration tests for ON++.

These tests run against a LIVE API server (uvicorn) and REAL SurrealDB,
with REAL native binaries (pdftotext, tesseract, pandoc). No mocks — every
component is exercised end-to-end.

Distinguishing property from integration tests:
- Integration tests mock external binaries and SurrealDB; they verify logic
  in isolation.
- System tests use real everything; they verify the full stack works together.

Requirements to run:
  - SurrealDB binary (surreal) available or SURREAL_TEST_URL set
  - Native binaries: pdftotext, tesseract, pandoc (optional; tests skip if missing)
  - Python packages: httpx, surrealdb, pypdf (for PDF fixture generation)

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
import time
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

API_PORT = 5099  # Avoid colliding with dev server on 5055


# ── Fixtures ──────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Find a free TCP port for the API server."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
async def api_base_url(surreal_db: str) -> str:
    """Start the FastAPI server (uvicorn) and yield its base URL.

    The server runs for the whole test session. Tests use httpx to make
    real HTTP requests against it.

    Depends on the root conftest's `surreal_db` fixture (session-scoped),
    which starts SurrealDB if a binary is available and creates the
    onpp_test/test namespace/database.
    """
    import os as _os

    env = dict(_os.environ)
    env["SURREAL_NAMESPACE"] = "onpp_test"
    env["SURREAL_DATABASE"] = "test"
    env["OPEN_NOTEBOOK_PASSWORD"] = ""
    env["OPEN_NOTEBOOK_REQUIRE_AUTH"] = "false"
    # Pin to system-test SurrealDB
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

    # Wait for the server to be ready
    base_url = url
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if proc.poll() is not None:
            # Server died
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
    """HTTP client against the running API server."""
    async with httpx.AsyncClient(base_url=api_base_url, timeout=30.0) as client:
        yield client


# ── Capability checks ────────────────────────────────────────────────────


def _has_binary(name: str) -> bool:
    """Check if a native binary is available."""
    import shutil

    return shutil.which(name) is not None


pytestmark_has_pdftotext = pytest.mark.skipif(
    not _has_binary("pdftotext"), reason="pdftotext not installed"
)
pytestmark_has_tesseract = pytest.mark.skipif(
    not _has_binary("tesseract"), reason="tesseract not installed"
)
pytestmark_has_pandoc = pytest.mark.skipif(
    not _has_binary("pandoc"), reason="pandoc not installed"
)
