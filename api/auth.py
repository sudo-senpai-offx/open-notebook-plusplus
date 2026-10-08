import os
import secrets
from typing import Optional

from fastapi import Request
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from open_notebook.utils.encryption import get_secret_from_env

#: Opt-in flag (B4): when truthy, the API refuses to start unless a password
#: is configured, instead of silently serving every request unauthenticated.
REQUIRE_AUTH_ENV = "OPEN_NOTEBOOK_REQUIRE_AUTH"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}


def require_auth_from_env() -> bool:
    """Whether the operator demanded authentication (default: off).

    Unset -> ``False``. Recognized truthy/falsy spellings are case-insensitive;
    anything else is treated as off (fail-open to the historical behaviour,
    matching the default-off design) with a warning.
    """
    raw = os.environ.get(REQUIRE_AUTH_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    logger.warning(
        f"{REQUIRE_AUTH_ENV}={raw!r} is not a recognized boolean; treating as off"
    )
    return False


def auth_is_configured() -> bool:
    """Whether an API password is configured (env or Docker secret file).

    This is exactly the condition under which ``PasswordAuthMiddleware``
    authenticates requests, so the guard and the middleware can never disagree.
    """
    return bool(get_secret_from_env("OPEN_NOTEBOOK_PASSWORD"))


def enforce_require_auth() -> None:
    """Fail fast when auth is demanded but no password is configured (B4).

    Called from the API lifespan before any other startup work so a
    ``OPEN_NOTEBOOK_REQUIRE_AUTH=true`` deployment cannot come up wide open by
    accident.
    """
    if require_auth_from_env() and not auth_is_configured():
        raise RuntimeError(
            f"{REQUIRE_AUTH_ENV} is enabled but no API password is configured. "
            "Set OPEN_NOTEBOOK_PASSWORD (or OPEN_NOTEBOOK_PASSWORD_FILE) to the "
            f"password clients must send, or unset {REQUIRE_AUTH_ENV} to run "
            "without authentication."
        )


class PasswordAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to check password authentication for all API requests.
    Auth is fully disabled (no hardcoded default password) if
    OPEN_NOTEBOOK_PASSWORD is not set.
    Supports Docker secrets via OPEN_NOTEBOOK_PASSWORD_FILE.
    """

    def __init__(
        self, app: ASGIApp, excluded_paths: Optional[list[str]] = None
    ) -> None:
        super().__init__(app)
        self.password = get_secret_from_env("OPEN_NOTEBOOK_PASSWORD")
        self.excluded_paths: list[str] = excluded_paths or [
            "/",
            "/health",
            "/docs",
            "/openapi.json",
            "/redoc",
        ]

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip authentication if no password is set
        if not self.password:
            return await call_next(request)

        # Skip authentication for excluded paths
        if request.url.path in self.excluded_paths:
            return await call_next(request)

        # Skip authentication for CORS preflight requests (OPTIONS)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Check authorization header
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Expected format: "Bearer {password}"
        try:
            scheme, credentials = auth_header.split(" ", 1)
            if scheme.lower() != "bearer":
                raise ValueError("Invalid authentication scheme")
        except ValueError:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid authorization header format"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Check password (constant-time to avoid a timing side-channel)
        if not secrets.compare_digest(
            credentials.encode("latin-1"), self.password.encode("utf-8")
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid password"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Password is correct, proceed with the request
        response = await call_next(request)
        return response
