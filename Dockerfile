FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

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

# Dependency layer: install from pyproject alone (src stubbed empty) so source
# edits don't invalidate this layer or the Chromium layer below; the real
# package lands in the final layer. Pip's download cache is a BuildKit mount,
# so it persists across builds without entering the image.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip mkdir -p src && pip install .

# Headless Chromium for the QA stage's rendered-post screenshots (worker) and
# future scraper adapters. install-deps pulls the required system libraries.
# Must stay after the pip layer: the browser build is tied to the installed
# playwright version, and installing it separately risks version skew.
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps .

EXPOSE 8200
