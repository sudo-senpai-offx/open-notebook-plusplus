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
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from surrealdb import AsyncSurreal

# ── Pin test namespace/database BEFORE any imports that read env ──
os.environ["SURREAL_NAMESPACE"] = "onpp_system_test"
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

from open_notebook.database.repository import get_database_password  # noqa: E402

API_PORT = 5099  # Avoid colliding with dev server on 5055


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def surreal_test_url() -> str:
    """Return a SurrealDB URL for system tests, starting one if possible."""
    import shutil

    binary = shutil.which("surreal")
    if not binary:
        for candidate in ("/usr/local/bin/surreal", "/opt/surrealdb/surreal", "/usr/bin/surreal"):
            import os as _os
            if _os.path.exists(candidate):
                binary = candidate
                break

    env_url = os.environ.get("SURREAL_TEST_URL")
    if binary:
        tmpdir = tempfile.mkdtemp(prefix="onpp-system-surreal-")
        db_path = f"rocksdb://{tmpdir}/db"
        proc = subprocess.Popen(
            [binary, "start", db_path, "--user", "root", "--pass", "root",
             "--bind", "127.0.0.1:18001"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_port("127.0.0.1", 18001, timeout=15.0)
            yield "ws://127.0.0.1:18001/rpc"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return

    if env_url:
        yield env_url
        return

    yield "ws://127.0.0.1:8000/rpc"


@pytest.fixture(scope="session")
async def surreal_db(surreal_test_url: str):
    """Ensure onpp_system_test/test namespace/database exist."""
    db = AsyncSurreal(surreal_test_url)
    try:
        await db.signin({
            "username": os.environ.get("SURREAL_USER", "root"),
            "password": get_database_password(),
        })
        await db.query("DEFINE NAMESPACE IF NOT EXISTS onpp_system_test")
        await db.use(namespace="onpp_system_test")
        await db.query("DEFINE DATABASE IF NOT EXISTS test")
        await db.use(namespace="onpp_system_test", database="test")
        yield surreal_test_url
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"SurrealDB unavailable at {surreal_test_url}: {exc}. "
            "Start SurrealDB or set SURREAL_TEST_URL."
        )
    finally:
        await db.close()


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """Wait until a TCP port accepts connections."""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


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
    """
    import os as _os

    env = dict(_os.environ)
    env["SURREAL_NAMESPACE"] = "onpp_system_test"
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


@pytest.fixture
async def db_query(surreal_test_url: str):
    """Direct SurrealDB query against onpp_system_test/test for state verification.

    Yields an async callable:

        rows = await db_query("SELECT * FROM source", {})
    """

    async def _query(sql: str, vars: dict | None = None) -> list[dict]:
        from surrealdb import RecordID as _RID

        db = AsyncSurreal(surreal_test_url)
        try:
            await db.signin({
                "username": os.environ.get("SURREAL_USER", "root"),
                "password": get_database_password(),
            })
            await db.use(namespace="onpp_system_test", database="test")
            safe_vars = {}
            if vars:
                for k, v in vars.items():
                    if isinstance(v, str) and ":" in v:
                        try:
                            safe_vars[k] = _RID.parse(v)
                        except Exception:
                            safe_vars[k] = v
                    else:
                        safe_vars[k] = v
            result = await db.query(sql, safe_vars)
            if isinstance(result, str):
                raise RuntimeError(f"SurrealDB error: {result}")
            return _normalize_record_ids(result)
        finally:
            await db.close()

    return _query


def _normalize_record_ids(obj):
    """Recursively convert SurrealDB RecordIDs to strings."""
    from surrealdb.data.types.record_id import RecordID as _RID

    if isinstance(obj, _RID):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _normalize_record_ids(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_record_ids(v) for v in obj]
    return obj


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
