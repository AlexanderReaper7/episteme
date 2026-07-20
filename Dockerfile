# uv's Debian-based image: python:3.12 on bookworm-slim (same base as the old
# python:3.12-slim) with uv preinstalled. Debian keeps apt working for the PGDG
# PostgreSQL client and `playwright install --with-deps` below — the Alpine/musl
# uv variants have neither apt nor bundled Python and break Chromium.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    # Cache mount is a different filesystem than the target, so copy rather than
    # hardlink to avoid uv's cross-device warning.
    UV_LINK_MODE=copy \
    # Compile .pyc at install time so container startup doesn't pay for it.
    UV_COMPILE_BYTECODE=1

# `uv sync` builds the project into /app/.venv; put it on PATH so the compose
# entrypoints (uvicorn / procrastinate / python -m ...) resolve from it.
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# PostgreSQL 18 client (pg_dump/pg_restore) for the worker's nightly backups.
# Debian ships an older major, so pull v18 from PGDG — it is >= the pg18 server
# (pg_dump refuses a newer server) and is built with zstd, so the backup task can
# --compress=zstd. Own layer, ahead of the code layers, so it stays cached.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer: sync from the lockfile with the project itself excluded
# (--no-install-project) and dev tooling dropped (--no-dev). Source edits don't
# touch pyproject.toml/uv.lock, so this layer and the Chromium layer below stay
# cached; only the final project-install layer rebuilds. uv's download cache is a
# BuildKit mount, so it persists across builds without entering the image.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# Headless Chromium for the QA stage's rendered-post screenshots (worker) and
# future scraper adapters. install-deps pulls the required system libraries.
# Must stay after the sync layer: the browser build is tied to the installed
# playwright version, and installing it separately risks version skew.
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

# Final layer: install the project itself against the already-synced deps.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

EXPOSE 8200
