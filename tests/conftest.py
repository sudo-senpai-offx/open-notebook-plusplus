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
