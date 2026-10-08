"""B4: ``OPEN_NOTEBOOK_REQUIRE_AUTH`` guard — fail fast instead of serving open.

The API is intentionally unauthenticated when no password is configured (local
dev, probes). This opt-in flag lets an operator demand the opposite: refuse to
start unless a password is actually set.
"""

import pytest

from api import main as api_main
from api.auth import (
    REQUIRE_AUTH_ENV,
    auth_is_configured,
    enforce_require_auth,
    require_auth_from_env,
)

PASSWORD_ENV = "OPEN_NOTEBOOK_PASSWORD"
PASSWORD_FILE_ENV = f"{PASSWORD_ENV}_FILE"


@pytest.mark.parametrize(
    "raw", [None, "", "0", "false", "no", "off", "OFF", "  false "]
)
def test_require_auth_off_by_default_and_for_falsy(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(REQUIRE_AUTH_ENV, raising=False)
    else:
        monkeypatch.setenv(REQUIRE_AUTH_ENV, raw)

    assert require_auth_from_env() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_require_auth_truthy(monkeypatch, raw):
    monkeypatch.setenv(REQUIRE_AUTH_ENV, raw)

    assert require_auth_from_env() is True


def test_require_auth_malformed_falls_back_to_off(monkeypatch):
    monkeypatch.setenv(REQUIRE_AUTH_ENV, "banana")

    assert require_auth_from_env() is False


def test_auth_is_configured_tracks_password_env(monkeypatch):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.delenv(PASSWORD_FILE_ENV, raising=False)
    assert auth_is_configured() is False

    monkeypatch.setenv(PASSWORD_ENV, "secret")
    assert auth_is_configured() is True


def test_auth_is_configured_accepts_docker_secret_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "password"
    secret_file.write_text("from-file\n")
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.setenv(PASSWORD_FILE_ENV, str(secret_file))

    assert auth_is_configured() is True


def test_enforce_raises_when_on_and_unconfigured(monkeypatch):
    monkeypatch.setenv(REQUIRE_AUTH_ENV, "true")
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.delenv(PASSWORD_FILE_ENV, raising=False)

    with pytest.raises(RuntimeError, match=REQUIRE_AUTH_ENV):
        enforce_require_auth()


def test_enforce_noop_when_on_and_configured(monkeypatch):
    monkeypatch.setenv(REQUIRE_AUTH_ENV, "true")
    monkeypatch.setenv(PASSWORD_ENV, "secret")

    enforce_require_auth()  # must not raise


def test_enforce_noop_when_off_even_without_password(monkeypatch):
    monkeypatch.delenv(REQUIRE_AUTH_ENV, raising=False)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.delenv(PASSWORD_FILE_ENV, raising=False)

    enforce_require_auth()  # must not raise


@pytest.mark.asyncio
async def test_lifespan_fails_before_database_work_when_auth_demanded(monkeypatch):
    monkeypatch.setenv(REQUIRE_AUTH_ENV, "true")
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.delenv(PASSWORD_FILE_ENV, raising=False)

    def _must_not_construct():
        raise AssertionError("migrations must not run when the auth guard fails")

    monkeypatch.setattr(api_main, "AsyncMigrationManager", _must_not_construct)

    with pytest.raises(RuntimeError, match=REQUIRE_AUTH_ENV):
        # lifespan never touches its app argument, so None keeps the test
        # isolated from the real FastAPI app.
        async with api_main.lifespan(None):  # type: ignore[arg-type]
            pass
