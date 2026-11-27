# Load environment variables
from dotenv import load_dotenv

from api.job_monitor import JobMonitor

load_dotenv()

# Keep the internal SurrealDB websocket out of any configured HTTP proxy
# (issue #1160). Must run after load_dotenv() (so a proxy set in .env is
# already visible) and before the DB is touched.
from open_notebook.utils.proxy import ensure_internal_no_proxy

ensure_internal_no_proxy()
try:
    from open_notebook.utils.resource_tuning import apply_tuning

    apply_tuning()
except Exception:
    pass

from open_notebook.preprocess.capabilities import get_capability_status

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from open_notebook.sync.scheduler import start_scheduler, stop_scheduler

import httpx

from api.auth import PasswordAuthMiddleware, enforce_require_auth
from api.middleware import MaxBodySizeMiddleware, get_max_upload_size_bytes
from api.routers import (
    assessments,
    auth,
    browser_ai,
    capabilities,
    chat,
    config,
    course,
    credentials,
    deepseek_sessions,
    deepseek_web,
    embedding,
    embedding_rebuild,
    episode_profiles,
    flashcards,
    focus,
    governor,
    insights,
    languages,
    metrics,
    models,
    notebooks,
    notebooks,
    notebook_templates,
    notebook_versions,
    notebook_merge,
    notes,
    podcasts,
    preprocessor,
    processing,
    prompt_library,
    providers,
    review,
    search,
    search_history,
    sessions,
    services,
    settings,
    sites,
    social,
    source_chat,
    storytelling,
    study,
    sources,
    speaker_profiles,
    source_links,
    sync,
    transformations,
    notebook_graph,
    tts_stt,
    webhooks,
    export,
)
from api.routers import commands as commands_router
from open_notebook.database.async_migrate import AsyncMigrationManager
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ExternalServiceError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    OpenNotebookError,
    RateLimitError,
    UnsupportedTypeException,
)
from open_notebook.utils.encryption import get_secret_from_env


def _parse_cors_origins(raw: str) -> list[str]:
    """Parse CORS_ORIGINS env value into a list of origins."""
    value = raw.strip()
    if value == "*":
        return ["*"]
    return [origin.strip() for origin in value.split(",") if origin.strip()]


# Parsed once at module load; CORS_ORIGINS changes require a restart.
_cors_origins_raw = os.getenv("CORS_ORIGINS")
CORS_ALLOWED_ORIGINS = _parse_cors_origins(_cors_origins_raw or "*")
CORS_IS_DEFAULT_WILDCARD = _cors_origins_raw is None
# Keyed on the parsed list, not on whether the env var was set: an operator
# who explicitly sets CORS_ORIGINS=* must get the same wildcard treatment as
# the default, or credentials would combine with a wildcard origin - the
# exact reflect-any-Origin behavior this flag exists to prevent.
CORS_ALLOW_CREDENTIALS = "*" not in CORS_ALLOWED_ORIGINS

# Parsed once at module load; OPEN_NOTEBOOK_MAX_UPLOAD_SIZE_MB changes require a restart.
MAX_UPLOAD_SIZE_BYTES = get_max_upload_size_bytes()

DATABASE_STARTUP_RETRY_ATTEMPTS = 12
DATABASE_STARTUP_RETRY_INITIAL_DELAY_SECONDS = 1
DATABASE_STARTUP_RETRY_MAX_DELAY_SECONDS = 5
# Per-probe ceiling so a hung connection cannot exceed the retry budget or
# block startup indefinitely. A probe that exceeds this is treated as a
# transient failure and retried like any other unreachable-database attempt.
DATABASE_STARTUP_RETRY_PROBE_TIMEOUT_SECONDS = 5


def _cors_headers(request: Request) -> dict[str, str]:
    """
    Build CORS headers for error responses.

    Mirrors Starlette CORSMiddleware behavior: reflects the request Origin
    when the origin is allowed (or when wildcard is configured, since
    browsers reject `Access-Control-Allow-Origin: *` combined with
    credentials). Omits `Access-Control-Allow-Origin` for disallowed
    origins so the browser blocks the error body from leaking cross-origin.
    Only claims Access-Control-Allow-Credentials when the real CORSMiddleware
    would (see its allow_credentials comment above) - otherwise error
    responses would grant credentialed access the success path doesn't.
    """
    origin = request.headers.get("origin")
    headers: dict[str, str] = {
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
    }
    if CORS_ALLOW_CREDENTIALS:
        headers["Access-Control-Allow-Credentials"] = "true"

    if origin and ("*" in CORS_ALLOWED_ORIGINS or origin in CORS_ALLOWED_ORIGINS):
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"

    return headers


# Import commands to register them in the API process
try:
    logger.info("Commands imported in API process")
except Exception as e:
    logger.error(f"Failed to import commands in API process: {e}")


async def _wait_for_database(migration_manager: AsyncMigrationManager) -> None:
    """
    Wait for SurrealDB to accept connections before running migrations.

    Docker Compose can start the API before the database name is resolvable. Keep
    migration errors fail-fast by only retrying this lightweight readiness probe.
    """
    attempts = max(1, DATABASE_STARTUP_RETRY_ATTEMPTS)
    delay = DATABASE_STARTUP_RETRY_INITIAL_DELAY_SECONDS

    for attempt in range(1, attempts + 1):
        try:
            await asyncio.wait_for(
                migration_manager.ping(),
                timeout=DATABASE_STARTUP_RETRY_PROBE_TIMEOUT_SECONDS,
            )
            if attempt > 1:
                logger.info(f"Database became reachable on attempt {attempt}")
            return
        except Exception as e:
            if attempt == attempts:
                logger.error(
                    f"Database did not become reachable after {attempts} attempts"
                )
                raise

            logger.warning(
                "Database is not reachable yet "
                f"(attempt {attempt}/{attempts}): {str(e)}. "
                f"Retrying in {delay:g} seconds..."
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, DATABASE_STARTUP_RETRY_MAX_DELAY_SECONDS)


async def _run_database_migrations() -> None:
    """Run startup database migrations after SurrealDB is reachable."""
    migration_manager = AsyncMigrationManager()
    await _wait_for_database(migration_manager)

    current_version = await migration_manager.get_current_version()
    logger.info(f"Current database version: {current_version}")

    if await migration_manager.needs_migration():
        logger.warning("Database migrations are pending. Running migrations...")
        await migration_manager.run_migration_up()
        new_version = await migration_manager.get_current_version()
        logger.success(
            f"Migrations completed successfully. Database is now at version {new_version}"
        )
    else:
        logger.info("Database is already at the latest version. No migrations needed.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event handler for the FastAPI application.
    Runs database migrations automatically on startup.
    """
    # Startup: Security checks
    logger.info("Starting API initialization...")

    # B4: fail fast if auth was demanded but no password is configured, so a
    # misconfigured deployment never comes up serving unauthenticated requests.
    enforce_require_auth()

    # Security check: Encryption key
    if not get_secret_from_env("OPEN_NOTEBOOK_ENCRYPTION_KEY"):
        logger.warning(
            "OPEN_NOTEBOOK_ENCRYPTION_KEY not set. "
            "API key encryption will fail until this is configured. "
            "Set OPEN_NOTEBOOK_ENCRYPTION_KEY to any secret string."
        )

    # Run database migrations

    try:
        await _run_database_migrations()
    except Exception as e:
        logger.error(f"CRITICAL: Database migration failed: {str(e)}")
        logger.exception(e)
        # Fail fast - don't start the API with an outdated database schema
        raise RuntimeError(f"Failed to run database migrations: {str(e)}") from e

    logger.success("API initialization completed successfully")
    # Load persisted DeepSeek-web credentials into the direct service singleton
    # so they are immediately available after a restart (no user re-entry needed).
    try:
        from open_notebook.ai.models import ProcessingFlags
        from open_notebook.services.deepseek_direct_service import (
            configure as configure_direct,
        )

        flags = await ProcessingFlags.get_instance()
        if flags.deepseek_ds_session_id and flags.deepseek_authorization_token:
            configure_direct(
                flags.deepseek_ds_session_id,
                flags.deepseek_authorization_token,
            )
            logger.info("DeepSeek-web credentials loaded from storage on startup")
    except Exception as e:  # noqa: BLE001 - credential load must never block boot
        logger.debug(f"DeepSeek-web credential load skipped: {e}")

    # Initialize provider registry with all configured providers
    try:
        from open_notebook.ai.provider_registry import initialize_providers

        await initialize_providers()
        logger.info("Provider registry initialized with all providers")
    except Exception as e:  # noqa: BLE001 - provider init must never block boot
        logger.debug(f"Provider registry init skipped: {e}")
    # T3 governor: a draining=true persisted by a crashed run must not
    # survive into this boot. Idempotent; safe to run on every startup.
    try:
        from open_notebook.domain.service_state import ServiceState

        await ServiceState.clear_on_boot()
    except Exception as e:
        logger.debug(f"service-state boot-clear skipped: {e}")
    # Start job monitor
    _job_monitor = JobMonitor()
    _job_monitor.start()
    # Admission dispatcher: promotes `held` jobs to `new` under the live
    # window and the memory gate. Runs here rather than as its own process
    # because the phone launcher supervises nothing - `nohup ... &` with no
    # restart policy - so a standalone dispatcher that died stayed dead and
    # looked exactly like a busy device.
    from open_notebook.scheduler.service import Dispatcher

    _dispatcher = Dispatcher()
    _dispatcher.start()
    app.state.dispatcher = _dispatcher
    # T3 governor (observe-only): evaluates hibernate verdicts and logs them.
    # Stops nothing; stop-order execution and wake are later slices.
    from api.governor import Governor

    _governor = Governor()
    _governor.start()
    app.state.governor = _governor
    # Start background sync scheduler (every 6 hours)
    await start_scheduler()
    # Resume-on-reopen: reconcile jobs interrupted by device restart.
    # Any command still in `running` was killed mid-execution; re-queue it
    # as `held` if attempts remain, or mark `failed` with a clear reason
    # when attempts are exhausted. Nothing is silently abandoned.
    try:
        from open_notebook.ai.models import JobCheckpoint

        interrupted = await repo_query(
            "SELECT id, name, args, error_message FROM command WHERE status = 'running'"
        )
        for row in interrupted or []:
            cmd_id = str(row["id"])
            checkpoint = await JobCheckpoint.get_for_command(cmd_id)
            attempts = checkpoint.attempt_count if checkpoint else 0
            # A job with no checkpoint has never been interrupted, so it
            # can be retried. Otherwise, check remaining attempts.
            if checkpoint is None:
                can = True
            else:
                can = await checkpoint.can_retry()
            if can:
                await repo_query(
                    "UPDATE $id SET status = $status, error_message = $error",
                    {
                        "id": ensure_record_id(cmd_id),
                        "status": "held",
                        "error": "resumed after device restart - re-queued",
                    },
                )
                await JobCheckpoint.upsert_for_command(
                    cmd_id,
                    increment_attempt=True,
                    checkpoint=checkpoint.checkpoint if checkpoint else {},
                )
                logger.info(f"resume: re-queued {cmd_id} (attempt {attempts + 1}/3)")
            else:
                await JobCheckpoint.upsert_for_command(
                    cmd_id,
                    increment_attempt=True,
                    checkpoint=checkpoint.checkpoint if checkpoint else {},
                )
                await repo_query(
                    "UPDATE $id SET status = $status, error_message = $error",
                    {
                        "id": ensure_record_id(cmd_id),
                        "status": "failed",
                        "error": "exhausted all 3 resume attempts after repeated interruption",
                    },
                )
                logger.warning(
                    f"resume: exhausted {cmd_id} after {attempts + 1} attempts"
                )
    except Exception as exc:  # noqa: BLE001 - resume must never block boot
        logger.debug(f"resume-on-reopen skipped: {exc}")
    # Reconcile `new` jobs: promoted by the dispatcher but never claimed by
    # the worker before the device restarted. Move back to `held` so the
    # dispatcher can re-admit them on the next pass.
    try:
        from open_notebook.ai.models import JobCheckpoint

        promoted_orphans = await repo_query(
            "SELECT id, name, args FROM command WHERE status = 'new'"
        )
        for row in promoted_orphans or []:
            cmd_id = str(row["id"])
            checkpoint = await JobCheckpoint.get_for_command(cmd_id)
            attempts = checkpoint.attempt_count if checkpoint else 0
            if checkpoint is None:
                can = True
            else:
                can = await checkpoint.can_retry()
            if can:
                await repo_query(
                    "UPDATE $id SET status = $status, error_message = $error",
                    {
                        "id": ensure_record_id(cmd_id),
                        "status": "held",
                        "error": "resumed after device restart - promoted but never claimed",
                    },
                )
                await JobCheckpoint.upsert_for_command(
                    cmd_id,
                    increment_attempt=True,
                    checkpoint=checkpoint.checkpoint if checkpoint else {},
                )
                logger.info(f"resume: re-queued new→held {cmd_id} (attempt {attempts + 1}/3)")
            else:
                await JobCheckpoint.upsert_for_command(
                    cmd_id,
                    increment_attempt=True,
                    checkpoint=checkpoint.checkpoint if checkpoint else {},
                )
                await repo_query(
                    "UPDATE $id SET status = $status, error_message = $error",
                    {
                        "id": ensure_record_id(cmd_id),
                        "status": "failed",
                        "error": "exhausted all 3 resume attempts after repeated interruption",
                    },
                )
                logger.warning(f"resume: exhausted new→held {cmd_id} after {attempts + 1} attempts")
    except Exception as exc:  # noqa: BLE001 - new-job reconcile must never block boot
        logger.debug(f"new-job reconciliation skipped: {exc}")
    # Yield control to the application
    yield
    await _job_monitor.stop()
    # The dispatcher stops before the monitor: it writes no terminal state, so
    # stopping it second leaves nothing half-promoted at exit.
    _dispatcher.stop()
    await _governor.stop()
    # Stop background sync scheduler
    await stop_scheduler()
    logger.info("API shutdown complete")


app = FastAPI(
    title="Open Notebook API",
    description="API for Open Notebook - Research Assistant",
    lifespan=lifespan,
)

if CORS_IS_DEFAULT_WILDCARD:
    logger.warning(
        "CORS_ORIGINS is not set — API accepts cross-origin requests from any "
        "origin (default: '*'). For production deployments, set CORS_ORIGINS to "
        "your frontend origin(s), e.g. "
        "CORS_ORIGINS=https://notebook.example.com"
    )
else:
    logger.info(f"CORS allowed origins: {CORS_ALLOWED_ORIGINS}")

# Add password authentication middleware first
# Exclude /api/auth/status and /api/config from authentication
app.add_middleware(
    PasswordAuthMiddleware,
    excluded_paths=[
        "/",
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/api/auth/status",
        "/api/config",
    ],
)

# Reject oversized request bodies before they reach auth or routing - added
# after PasswordAuthMiddleware (so it wraps around it) so a too-large request
# is rejected before spending any work checking credentials.
logger.info(
    f"Max request body size: {MAX_UPLOAD_SIZE_BYTES / (1024 * 1024):g}MB "
    "(set OPEN_NOTEBOOK_MAX_UPLOAD_SIZE_MB to change)"
)
app.add_middleware(MaxBodySizeMiddleware, max_body_size=MAX_UPLOAD_SIZE_BYTES)

# Add CORS middleware last (so it processes first, and so it can attach
# CORS headers to a 413 raised by MaxBodySizeMiddleware)
#
# allow_credentials is tied to whether CORS_ORIGINS resolves to specific
# origins: combining allow_origins=["*"] with allow_credentials=True makes
# Starlette reflect the request's Origin header verbatim (browsers reject a
# literal "*" alongside credentials), which defeats the origin allowlist.
# The frontend never sends credentialed requests (withCredentials: false)
# and auth is a Bearer header, not a cookie, so this isn't independently
# exploitable today - but there's no reason to allow it for any wildcard
# case. Once an operator explicitly scopes CORS_ORIGINS to real origins,
# credentialed cross-origin requests to those origins are safe to allow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request/Response logging middleware ────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    import time as _time

    start = _time.monotonic()
    response = await call_next(request)
    duration_ms = int((_time.monotonic() - start) * 1000)
    logger.info(
        f"{request.method} {request.url.path} -> {response.status_code} ({duration_ms}ms)"
    )
    return response


# ── Rate limiting middleware (token bucket per client IP) ──────────────────
class _RateLimiter:
    """Simple in-memory token bucket rate limiter per client IP."""

    def __init__(self, requests_per_minute: int = 120):
        self.capacity = requests_per_minute
        self.refill_rate = requests_per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, client_ip: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            tokens, last_refill = self._buckets.get(
                client_ip, (self.capacity, now)
            )
            elapsed = now - last_refill
            tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
            if tokens >= 1.0:
                tokens -= 1.0
                self._buckets[client_ip] = (tokens, now)
                return True
            self._buckets[client_ip] = (tokens, now)
            return False


_rate_limiter = _RateLimiter(
    requests_per_minute=int(
        os.environ.get("OPEN_NOTEBOOK_API_RATE_LIMIT", "120")
    )
)


@app.middleware("http")
async def rate_limit_requests(request: Request, call_next):
    if request.url.path in ("/health", "/api/auth/status"):
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    if not await _rate_limiter.is_allowed(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down."},
            headers=_cors_headers(request),
        )
    return await call_next(request)


# Custom exception handler to ensure CORS headers are included in error responses
# This helps when errors occur before the CORS middleware can process them
@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Custom exception handler that ensures CORS headers are included in error responses.
    This is particularly important for 413 (Payload Too Large) errors during file uploads.

    Note: If a reverse proxy (nginx, traefik) returns 413 before the request reaches
    FastAPI, this handler won't be called. In that case, configure your reverse proxy
    to add CORS headers to error responses.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers={**(exc.headers or {}), **_cors_headers(request)},
    )


@app.exception_handler(NotFoundError)
async def not_found_error_handler(request: Request, exc: NotFoundError):
    return JSONResponse(
        status_code=404,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(InvalidInputError)
async def invalid_input_error_handler(request: Request, exc: InvalidInputError):
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(request: Request, exc: AuthenticationError):
    return JSONResponse(
        status_code=401,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(RateLimitError)
async def rate_limit_error_handler(request: Request, exc: RateLimitError):
    return JSONResponse(
        status_code=429,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(ConfigurationError)
async def configuration_error_handler(request: Request, exc: ConfigurationError):
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(NetworkError)
async def network_error_handler(request: Request, exc: NetworkError):
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(ExternalServiceError)
async def external_service_error_handler(request: Request, exc: ExternalServiceError):
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(UnsupportedTypeException)
async def unsupported_type_error_handler(
    request: Request, exc: UnsupportedTypeException
):
    return JSONResponse(
        status_code=415,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


@app.exception_handler(OpenNotebookError)
async def open_notebook_error_handler(request: Request, exc: OpenNotebookError):
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers=_cors_headers(request),
    )


# Include routers
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(browser_ai.router, prefix="/api", tags=["browser-ai"])
app.include_router(config.router, prefix="/api", tags=["config"])
app.include_router(notebooks.router, prefix="/api", tags=["notebooks"])
app.include_router(notebook_templates.router, prefix="/api", tags=["notebook-templates"])
app.include_router(notebook_versions.router, prefix="/api", tags=["notebook-versions"])
app.include_router(notebook_merge.router, prefix="/api", tags=["notebook-merge"])
app.include_router(search.router, prefix="/api", tags=["search"])
app.include_router(search_history.router, prefix="/api", tags=["search-history"])
app.include_router(services.router, prefix="/api", tags=["services"])
app.include_router(deepseek_web.router, prefix="/api", tags=["deepseek-web"])
app.include_router(deepseek_sessions.router, prefix="/api", tags=["deepseek-sessions"])
app.include_router(tts_stt.router, prefix="/api", tags=["tts-stt"])
app.include_router(models.router, prefix="/api", tags=["models"])
app.include_router(transformations.router, prefix="/api", tags=["transformations"])
app.include_router(notes.router, prefix="/api", tags=["notes"])
app.include_router(embedding.router, prefix="/api", tags=["embedding"])
app.include_router(
    embedding_rebuild.router, prefix="/api/embeddings", tags=["embeddings"]
)
app.include_router(governor.router, prefix="/api", tags=["governor"])
app.include_router(focus.router, prefix="/api", tags=["focus"])
app.include_router(flashcards.router, prefix="/api", tags=["flashcards"])
app.include_router(sync.router, prefix="/api", tags=["sync"])
app.include_router(processing.router, prefix="/api", tags=["processing"])
app.include_router(review.router, prefix="/api", tags=["review"])
app.include_router(metrics.router, prefix="/api", tags=["metrics"])
app.include_router(settings.router, prefix="/api", tags=["settings"])
app.include_router(assessments.router, prefix="/api", tags=["assessments"])
app.include_router(sources.router, prefix="/api", tags=["sources"])
app.include_router(sites.router, prefix="/api", tags=["sites"])
app.include_router(social.router, prefix="/api", tags=["social"])
app.include_router(storytelling.router, prefix="/api", tags=["storytelling"])
app.include_router(study.router, prefix="/api", tags=["study"])
app.include_router(insights.router, prefix="/api", tags=["insights"])
app.include_router(commands_router.router, prefix="/api", tags=["commands"])
app.include_router(podcasts.router, prefix="/api", tags=["podcasts"])
app.include_router(preprocessor.router, prefix="/api", tags=["preprocessor"])
app.include_router(prompt_library.router, prefix="/api", tags=["prompt-library"])
app.include_router(episode_profiles.router, prefix="/api", tags=["episode-profiles"])
app.include_router(speaker_profiles.router, prefix="/api", tags=["speaker-profiles"])
app.include_router(source_links.router, prefix="/api", tags=["source-links"])
app.include_router(notebook_graph.router, prefix="/api", tags=["notebook-graph"])
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(source_chat.router, prefix="/api", tags=["source-chat"])
app.include_router(course.router, prefix="/api", tags=["course"])
app.include_router(credentials.router, prefix="/api", tags=["credentials"])
app.include_router(providers.router, prefix="/api", tags=["providers"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
app.include_router(capabilities.router, prefix="/api", tags=["capabilities"])
app.include_router(languages.router, prefix="/api", tags=["languages"])
app.include_router(webhooks.router, prefix="/api", tags=["webhooks"])
app.include_router(export.router, prefix="/api", tags=["export"])


@app.get("/")
async def root():
    return {"message": "Open Notebook API is running"}


@app.get("/health")
async def health():
    """Health check. Verifies the API process is alive and SurrealDB is reachable.

    Also reports vector/FTS index sizes and table counts for restore
    verification (Batch E: restore-verifiable guarantee).
    """
    from open_notebook.database.async_migrate import AsyncMigrationManager

    db_ok = False
    index_info = {}
    table_counts = {}
    try:
        mm = AsyncMigrationManager()
        await mm.ping()
        db_ok = True

        # Report index sizes for restore verification
        from open_notebook.database.repository import repo_query

        try:
            vec_size = await repo_query(
                "SELECT sum(array::len(embedding)) * 8 as vec_bytes FROM source_chunk WHERE embedding != NONE GROUP ALL"
            )
            if vec_size and vec_size[0].get("vec_bytes"):
                index_info["vector_index_bytes"] = int(vec_size[0]["vec_bytes"])
        except Exception:
            pass

        try:
            fts_count = await repo_query(
                "SELECT count() as c FROM source_chunk GROUP ALL"
            )
            if fts_count:
                index_info["fts_chunk_count"] = int(fts_count[0]["c"])
        except Exception:
            pass

        # Table counts for restore verification
        for table in [
            "source",
            "source_chunk",
            "flashcard",
            "review_history",
            "notebook",
            "note",
            "social_post",
            "transformation_run",
        ]:
            try:
                cnt = await repo_query(f"SELECT count() as c FROM {table} GROUP ALL")
                if cnt:
                    table_counts[table] = int(cnt[0]["c"])
            except Exception:
                pass
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "reachable" if db_ok else "unreachable",
        "vector_index_mb": round(index_info.get("vec_bytes", 0) / (1024 * 1024), 2),
        "fts_chunk_count": index_info.get("fts_chunk_count", 0),
        "table_counts": table_counts,
    }


@app.get("/health/ready")
async def readiness():
    """Readiness probe: dispatcher heartbeat + SurrealDB reachability.

    The dispatcher is critical because chat jobs submitted via POST /api/chat
    must be promoted from `held` to `new` before they can execute. If the
    dispatcher is stale (>90s since last heartbeat), chat appears broken.
    Returns 503 so load balancers and sidecars stop routing traffic.

    Non-critical deps (Ollama) degrade to 200 + degraded body, not 503.
    """
    from datetime import datetime, timezone

    from open_notebook.database.repository import repo_query

    # 1. SurrealDB must be reachable
    try:
        mm = AsyncMigrationManager()
        await mm.ping()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "db_unavailable"},
        )

    # 2. Dispatcher heartbeat must be recent (< 90s)
    try:
        rows = await repo_query(
            "SELECT last_updated_at FROM dispatcher_heartbeat LIMIT 1",
        )
    except Exception:
        # Table may not exist yet (migration 81 not applied) — treat as stale
        return JSONResponse(
            status_code=503,
            content={"status": "starting"},
        )

    if not rows:
        return JSONResponse(
            status_code=503,
            content={"status": "starting"},
        )

    last_updated = rows[0].get("last_updated_at")
    if last_updated is None:
        return JSONResponse(
            status_code=503,
            content={"status": "starting"},
        )

    # SurrealDB returns datetime objects directly; handle both str and datetime
    if isinstance(last_updated, str):
        last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
    else:
        last_dt = last_updated

    age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
    if age_s > 90:
        return JSONResponse(
            status_code=503,
            content={
                "status": "dispatcher_stale",
                "last_heartbeat_at": rows[0].get("last_updated_at"),
                "age_seconds": round(age_s, 1),
            },
        )

    return {
        "status": "ok",
        "capabilities": get_capability_status().to_dict(),
    }


@app.get("/api/ollama/models")
async def list_ollama_models():
    """List available Ollama models from the local server."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        models = await ollama_provider.list_models()
        return {"models": models}
    except Exception as e:
        logger.error(f"Error listing Ollama models: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error listing Ollama models: {str(e)}"
        )


@app.get("/api/test-ollama")
async def test_ollama_route():
    """Test route to verify ollama endpoint registration."""
    return {"status": "ok", "message": "ollama route is working"}


# ============================================================================
# Ollama on-demand model management endpoints
# ============================================================================

class _OllamaPullRequest(BaseModel):
    model: str


class _OllamaUnloadRequest(BaseModel):
    model: Optional[str] = None


class _OllamaKeepAliveRequest(BaseModel):
    model: Optional[str] = None
    duration: str = "0s"


class _OllamaEnsureLoadedRequest(BaseModel):
    model: Optional[str] = None
    auto_pull: bool = True


class _OllamaEmbedRequest(BaseModel):
    model: str
    input: str


@app.post("/api/ollama/pull")
async def ollama_pull_model(req: _OllamaPullRequest):
    """Pull (download) an Ollama model on-demand."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        result = await ollama_provider.pull_model(req.model)
        if result["status"] == "error":
            raise HTTPException(status_code=500, detail=result.get("error", "pull failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error pulling Ollama model {req.model}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ollama/unload")
async def ollama_unload_model(req: _OllamaUnloadRequest):
    """Unload an Ollama model from memory to free RAM."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        success = await ollama_provider.unload_model(req.model)
        return {"status": "unloaded" if success == True else "failed", "model": req.model}
    except Exception as e:
        logger.error(f"Error unloading Ollama model: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ollama/unload-all")
async def ollama_unload_all_models():
    """Unload all Ollama models from memory."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        success = await ollama_provider.unload_all_models()
        return {"status": "unloaded_all" if success else "failed"}
    except Exception as e:
        logger.error(f"Error unloading all Ollama models: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ollama/keep-alive")
async def ollama_set_keep_alive(req: _OllamaKeepAliveRequest):
    """Set keep_alive duration for an Ollama model."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        success = await ollama_provider.set_keep_alive(req.model, req.duration)
        return {"status": "ok" if success else "failed", "model": req.model, "duration": req.duration}
    except Exception as e:
        logger.error(f"Error setting keep_alive: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ollama/loaded")
async def ollama_list_loaded_models():
    """List models currently loaded in Ollama memory."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        base_url = await ollama_provider._get_base_url()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url}/api/ps")
            if resp.status_code == 200:
                return resp.json()
            return {"models": []}
    except Exception as e:
        logger.error(f"Error listing loaded Ollama models: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ollama/ensure-loaded")
async def ollama_ensure_loaded(req: _OllamaEnsureLoadedRequest):
    """Ensure an Ollama model is loaded in memory (pulls if missing)."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        result = await ollama_provider.ensure_model_loaded(req.model, auto_pull=req.auto_pull)
        if result["status"] == "error":
            raise HTTPException(status_code=500, detail=result.get("error", "ensure failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error ensuring Ollama model loaded: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ollama/embed")
async def ollama_embed(req: _OllamaEmbedRequest):
    """Generate embeddings using a local Ollama embedding model."""
    try:
        from open_notebook.services.ollama_provider import ollama_provider
        base_url = await ollama_provider._get_base_url()
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/api/embed",
                json={"model": req.model, "input": req.input},
            )
            if resp.status_code == 200:
                return resp.json()
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Ollama embed failed: {resp.text[:200]}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating Ollama embeddings: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
