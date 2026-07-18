FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

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
