# Single source of truth for both published image variants:
#   - regular (multi-container, SurrealDB external): default build / --target runtime
#   - single-container (app + SurrealDB):            --target single
# Shared stages below guarantee that fixes (tiktoken pre-cache, env defaults,
# npm retry logic, ...) apply to both variants at once.

# Stage 1: Frontend builder
FROM node:22-slim AS frontend-builder
WORKDIR /app/frontend

# Copy dependency files first to leverage cache
COPY frontend/package.json frontend/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN npm config set registry ${NPM_REGISTRY} \
 && npm config set fetch-retries 5 \
 && npm config set fetch-retry-mintimeout 20000 \
 && npm config set fetch-retry-maxtimeout 120000
# Retry npm ci to survive transient registry ECONNRESETs, which are common on
# the QEMU-emulated arm64 leg of the multi-arch build.
RUN i=0; until npm ci; do \
      i=$((i+1)); \
      if [ "$i" -ge 5 ]; then echo "npm ci failed after $i attempts"; exit 1; fi; \
      echo "npm ci failed (attempt $i); retrying in 15s"; sleep 15; \
    done

# Copy the rest of the frontend source and build
COPY frontend/ ./
RUN npm run build

# Stage 2: Backend builder
FROM python:3.14-slim-trixie AS backend-builder

# Install build dependencies (uv downloads pre-built wheels for most packages)
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv using the official method
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Set build optimization environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_HTTP_TIMEOUT=120
# Cap parallel downloads: under Docker Desktop's embedded DNS (192.168.65.7)
# uv's default burst of simultaneous lookups returns EAI_AGAIN, failing the
# whole venv download. A small pool keeps DNS traffic burst-free and reliable.
ENV UV_CONCURRENT_DOWNLOADS=8

# Copy dependency files and minimal package structure first for better layer caching
COPY pyproject.toml uv.lock ./
COPY open_notebook/__init__.py ./open_notebook/__init__.py

# Install dependencies (this layer is cached unless dependencies change)
# Retry to survive transient DNS/registry failures (same pattern as npm ci
# above); uv's wheel cache persists across attempts within this layer, so a
# flaky lookup on files.pythonhosted.org does not mean re-downloading wheels.
RUN i=0; until uv sync --frozen --no-dev; do \
      i=$((i+1)); \
      if [ "$i" -ge 5 ]; then echo "uv sync failed after $i attempts"; exit 1; fi; \
      echo "uv sync failed (attempt $i); retrying in 15s"; sleep 15; \
    done

# Pre-download tiktoken encoding so the app works offline (issue #264).
# /app/tiktoken-cache is intentionally outside /app/data/ so that volume mounts
# of /app/data (for user data persistence) do not hide the pre-baked encoding.
# config.py reads TIKTOKEN_CACHE_DIR from the environment to pick up this path.
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken-cache
RUN mkdir -p /app/tiktoken-cache && \
    .venv/bin/python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

# Stage 3: SurrealDB binary (pinned to v2 to match docker-compose.yml; used by the single target only)
FROM surrealdb/surrealdb:v2 AS surreal-binary

# Stage 4: Shared runtime base (everything common to both variants)
FROM python:3.14-slim-trixie AS runtime-base

# Install runtime system dependencies
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ffmpeg \
    supervisor \
    curl \
    chromium \
    libglib2.0-0 \
    libgl1 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# OCR for scanned PDFs (see open_notebook/ocr/). A separate layer so a
# flaky mirror on the upgrade above can neither block it nor be blocked
# by it: this fetches three small packages, not the world.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-jpn \
    && rm -rf /var/lib/apt/lists/*

# Install uv using the official method
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy the virtual environment from the backend builder
COPY --from=backend-builder /app/.venv /app/.venv

# Note: Docling / Crawl4AI are NOT pre-installed here. They are opt-in heavy
# runtimes (see docs/7-DEVELOPMENT/decisions/ADR-007-optin-runtimes.md): the
# compose profile keeps OPEN_NOTEBOOK_ENABLE_DOCLING / CRAWL4AI off by default,
# and scripts/docker-entrypoint.sh installs them on first boot only when those
# flags are turned on (cached on /app/data). Installing them unconditionally
# here would add several GB (ML stack, OCR, Chromium) to every image for
# functionality the local profile never uses.

# Copy the source code
COPY . /app

# Copy pre-downloaded tiktoken encoding from builder (outside /data/ — volume-mount safe)
COPY --from=backend-builder /app/tiktoken-cache /app/tiktoken-cache

# Copy built frontend from standalone output
COPY --from=frontend-builder /app/frontend/.next/standalone /app/frontend/
COPY --from=frontend-builder /app/frontend/.next/static /app/frontend/.next/static
COPY --from=frontend-builder /app/frontend/public /app/frontend/public
COPY --from=frontend-builder /app/frontend/start-server.js /app/frontend/start-server.js

# Ensure uv uses the existing venv without attempting network operations
ENV UV_NO_SYNC=1
ENV VIRTUAL_ENV=/app/.venv
# Point the app at the pre-baked tiktoken encoding (see open_notebook/config.py)
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken-cache
# Bind the API to all interfaces (IPv4). Set API_HOST=:: for IPv6 dual-stack environments
ENV API_HOST=0.0.0.0

# Caches for the opt-in heavy extraction runtimes (Docling, Crawl4AI local).
# These live UNDER /app/data so the user's volume mount persists them across
# container restarts and upgrades: wheels (uv), the Chromium browser (playwright)
# and Docling's ML models (huggingface) are downloaded once, then reused.
# See scripts/docker-entrypoint.sh and docs/7-DEVELOPMENT/decisions/ADR-007-optin-runtimes.md.
ENV UV_CACHE_DIR=/app/data/.cache/uv
ENV PLAYWRIGHT_BROWSERS_PATH=/app/data/.cache/playwright
ENV HF_HOME=/app/data/.cache/huggingface

# Data directory (volume-mounted by users) and supervisor log directory
RUN mkdir -p /app/data /var/log/supervisor \
    && chmod +x /app/scripts/wait-for-api.sh /app/scripts/docker-entrypoint.sh \
    /app/scripts/worker-launch.sh

# Copy supervisord configuration (shared programs: api, worker, frontend)
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose ports for Frontend and API
EXPOSE 8502 5055

# Runtime API URL Configuration
# The API_URL environment variable can be set at container runtime to configure
# where the frontend should connect to the API. This allows the same Docker image
# to work in different deployment scenarios without rebuilding.
#
# If not set, the system will auto-detect based on incoming requests.
# Set API_URL when using reverse proxies or custom domains.
#
# Example: docker run -e API_URL=https://your-domain.com/api ...

# The entrypoint installs any opt-in heavy runtimes (Docling, Crawl4AI local)
# enabled via OPEN_NOTEBOOK_ENABLE_* before handing off to CMD (supervisord).
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]

# Stage 5: Single-container variant (adds SurrealDB on top of the shared runtime)
# Build with: docker build --target single .
FROM runtime-base AS single

# Install SurrealDB (copied from pinned v2 image to match docker-compose.yml)
COPY --from=surreal-binary /surreal /usr/local/bin/surreal

# SurrealDB data directory (volume-mounted by users)
RUN mkdir -p /mydata

# Enable the surrealdb program in supervisord (appended to the shared config)
RUN cat /app/supervisord.surrealdb.conf >> /etc/supervisor/conf.d/supervisord.conf

# Stage 6 (default): Regular multi-container image (SurrealDB runs externally).
# Kept last so a plain `docker build .` produces this variant.
FROM runtime-base AS runtime
