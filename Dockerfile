FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Headless Chromium for the QA stage's rendered-post screenshots (worker) and
# future scraper adapters. install-deps pulls the required system libraries.
RUN playwright install --with-deps chromium

EXPOSE 8200
