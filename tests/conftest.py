"""
Pytest configuration file.

This file ensures that the project root is in the Python path,
allowing tests to import from the api and open_notebook modules.
"""

import os
import sys
from pathlib import Path

# Ensure password auth is disabled for tests BEFORE any imports
# The PasswordAuthMiddleware skips auth when this env var is not set
# Set to empty string instead of deleting to prevent it from being reloaded
os.environ["OPEN_NOTEBOOK_PASSWORD"] = ""

# ...and the *demand* for auth has to be off too, not just the credential.
# `load_dotenv` does not override variables already set, so pinning the
# password above survives the .env load - but OPEN_NOTEBOOK_REQUIRE_AUTH was
# never pinned, so the repository's real deployment setting came through and
# `enforce_require_auth()` raised in the API lifespan, failing tests that
# were asserting on migrations and never reached them.
#
# This is the general shape of the problem: the suite loads the developer's
# real `.env`, so any deployment-only setting becomes a test input. Two more
# are pinned here rather than papered over in the individual tests that
# happened to trip over them:
#
#   OPEN_NOTEBOOK_MODEL_POOL_BYTES - the PC's real value, which made the
#     admission gate compare every fixture against a 15 GB pool.
#   OPEN_NOTEBOOK_WINDOW_CAP - the phone's cap, irrelevant on a PC test run.
#
# Set to empty, not popped. `load_dotenv` does not override a variable that
# already exists, so popping here would just let the .env value straight back
# in - which is why an earlier version of this comment claimed a fix that
# was not happening. Both readers treat an empty value as unset and fall
# back to their own default.
os.environ["OPEN_NOTEBOOK_REQUIRE_AUTH"] = "false"
os.environ["OPEN_NOTEBOOK_MODEL_POOL_BYTES"] = ""
os.environ["OPEN_NOTEBOOK_WINDOW_CAP"] = ""

# Load environment variables from .env file
# This must be done BEFORE any imports that depend on environment variables
from dotenv import load_dotenv

# Load .env file from project root
dotenv_path = Path(__file__).parent.parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)
    print(f"Loaded environment variables from {dotenv_path}")
else:
    print(f"Warning: .env file not found at {dotenv_path}")

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


import pytest


@pytest.fixture(autouse=True)
def _isolate_data_folder(tmp_path_factory, request):
    """Keep the suite out of the developer's own data directory.

    Not tidiness. `import_all` calls `write_watermark()`, which reads
    `DATA_FOLDER` and writes a file there. Three test files exercise the
    sync importer, and with `DATA_FOLDER` unset they wrote into the
    repository's `data/` - where they had already left a watermark of
    2099, from a fixture that patched `read_watermark` to a far-future
    date.

    That left the real PC with the sweep fully armed, so every manifest
    entry looked deletable. The failure mode the watermark exists to
    prevent, delivered by its own test suite.

    Tests that need a specific DATA_FOLDER set it themselves after this
    runs; `monkeypatch` undo order is reverse of assignment, so their
    value wins for the duration of the test.
    """
    original = os.environ.get("DATA_FOLDER")
    os.environ["DATA_FOLDER"] = str(tmp_path_factory.mktemp("data"))
    yield
    if original is None:
        os.environ.pop("DATA_FOLDER", None)
    else:
        os.environ["DATA_FOLDER"] = original
