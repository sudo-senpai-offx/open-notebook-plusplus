"""
Pytest configuration for ON++ integration tests.

Sets up:
  - SURREAL_NAMESPACE=onpp_test, SURREAL_DATABASE=test (test isolation)
  - Ephemeral SurrealDB instance (session-scoped, CI)
  - Direct SurrealDB query fixture for state verification
  - Safety assertion refusing to run if not on test DB

SurrealDB is started in-memory (--memory) or in a temp directory
(rocksdb://) depending on availability. If neither works, tests that
require a real DB are skipped via the `surreal_db` fixture.

Tests that monkeypatch repo_query do NOT need a real SurrealDB and can
run without the `surreal_db` fixture.
"""

# ── Pin test namespace/database BEFORE any imports that read env ────────
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["SURREAL_NAMESPACE"] = "onpp_test"
os.environ["SURREAL_DATABASE"] = "test"

# Disable auth for tests
os.environ.setdefault("OPEN_NOTEBOOK_PASSWORD", "")
os.environ.setdefault("OPEN_NOTEBOOK_REQUIRE_AUTH", "false")

# Load .env from project root but do NOT override our test pins
from dotenv import load_dotenv

_dotenv_path = Path(__file__).parent / ".env"
if _dotenv_path.exists():
    load_dotenv(_dotenv_path, override=False)

# Add the project root to the Python path
_project_root = Path(__file__).parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import pytest  # noqa: E402
from surrealdb import AsyncSurreal  # noqa: E402

from open_notebook.database.repository import (  # noqa: E402
    get_database_url,
    get_database_password,
)


def _find_surreal_binary() -> str | None:
    """Locate the SurrealDB binary, if installed."""
    import shutil

    path = shutil.which("surreal")
    if path:
        return path
    for candidate in ("/usr/local/bin/surreal", "/opt/surrealdb/surreal", "/usr/bin/surreal"):
        if Path(candidate).exists():
            return candidate
    return None


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


# ── Ephemeral SurrealDB (session-scoped, opt-in) ────────────────────────

@pytest.fixture(scope="session")
def surreal_test_url() -> str:
    """Return a SurrealDB URL for tests, starting one if possible.

    Tries in-memory mode first (fastest, no disk I/O), then falls back
    to a temp-directory rocksdb instance. If no SurrealDB binary exists,
    returns the URL from SURREAL_TEST_URL env var (for CI-provided DB)
    or the default localhost:8000.
    """
    binary = _find_surreal_binary()
    if binary:
        tmpdir = tempfile.mkdtemp(prefix="onpp-test-surreal-")
        db_path = f"rocksdb://{tmpdir}/db"
        proc = subprocess.Popen(
            [binary, "start", db_path, "--user", "root", "--pass", "root",
             "--bind", "127.0.0.1:18000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if _wait_for_port("127.0.0.1", 18000, timeout=15.0):
            yield "ws://127.0.0.1:18000/rpc"
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return
        proc.terminate()
        proc.wait(timeout=5)

    # Fall back to env-provided URL (CI may run its own SurrealDB)
    env_url = os.environ.get("SURREAL_TEST_URL")
    if env_url:
        yield env_url
        return

    # Last resort: default local SurrealDB
    yield "ws://127.0.0.1:8000/rpc"


@pytest.fixture(scope="session")
async def surreal_db(surreal_test_url):
    """Ensure onpp_test/test namespace/database exist in SurrealDB.

    Skips all tests in the session if SurrealDB is unreachable.
    Tests that don't need a real DB should not request this fixture.
    """
    db = AsyncSurreal(surreal_test_url)
    try:
        await db.signin({
            "username": os.environ.get("SURREAL_USER", "root"),
            "password": get_database_password(),
        })
        await db.query("DEFINE NAMESPACE IF NOT EXISTS onpp_test")
        await db.use(namespace="onpp_test")
        await db.query("DEFINE DATABASE IF NOT EXISTS test")
        await db.use(namespace="onpp_test", database="test")
        yield surreal_test_url
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"SurrealDB unavailable at {surreal_test_url}: {exc}. "
            "Start SurrealDB or set SURREAL_TEST_URL."
        )
    finally:
        await db.close()


# ── Direct DB query fixture (test namespace) ────────────────────────────

@pytest.fixture
async def db_query(surreal_db, surreal_test_url):
    """Direct SurrealDB query against onpp_test/test for state verification.

    Requires surreal_db (real connection). Yields an async callable:

        rows = await db_query("SELECT * FROM command WHERE id = $id",
                              {"id": "command:abc"})

    Returns a list of dicts with RecordIDs normalized to strings.
    """

    async def _query(sql: str, vars: dict | None = None) -> list[dict]:
        db = AsyncSurreal(surreal_test_url)
        try:
            await db.signin({
                "username": os.environ.get("SURREAL_USER", "root"),
                "password": get_database_password(),
            })
            await db.use(namespace="onpp_test", database="test")
            safe_vars = {}
            if vars:
                from surrealdb import RecordID as _RID
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
    from surrealdb.data.types.record_id import RecordID as _RID  # type: ignore

    if isinstance(obj, _RID):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _normalize_record_ids(v) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) == 1 and isinstance(obj[0], list):
            return [_normalize_record_ids(v) for v in obj[0]]
        return [_normalize_record_ids(v) for v in obj]
    return obj


# ── Surgical ID tracking for cleanup ────────────────────────────────────

@pytest.fixture
async def tracked_records(surreal_db, surreal_test_url):
    """Track record IDs created during a test for surgical cleanup.

    Yields a set that tests add record IDs to. After the test, all tracked
    records are deleted. Never touches pre-existing data.
    """
    ids: set[str] = set()
    yield ids

    if ids:
        db = AsyncSurreal(surreal_test_url)
        try:
            await db.signin({
                "username": os.environ.get("SURREAL_USER", "root"),
                "password": get_database_password(),
            })
            await db.use(namespace="onpp_test", database="test")
            for rec_id in ids:
                try:
                    await db.query(f"DELETE {rec_id}")
                except Exception:
                    pass
        finally:
            await db.close()


# ── Safety assertion: refuse if not on test DB ──────────────────────────

@pytest.fixture(autouse=True)
def assert_test_db():
    """Refuse to run if the active DB is not 'test'."""
    ns = os.environ.get("SURREAL_NAMESPACE", "")
    db_name = os.environ.get("SURREAL_DATABASE", "")
    assert ns == "onpp_test", (
        f"SURREAL_NAMESPACE={ns!r}, expected 'onpp_test'."
    )
    assert db_name == "test", (
        f"SURREAL_DATABASE={db_name!r}, expected 'test'."
    )
