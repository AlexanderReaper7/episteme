# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Episteme: a self-hosted, single-user, LLM-powered personalized science newsfeed. Sources are
ingested continuously; a local LLM (llama-server on the host, port 5001) processes them
overnight into generated articles plus a Google-News-style aggregation stream.

**Read [episteme-architecture.md](episteme-architecture.md) first** — it is the authoritative
spec (data model, pipeline stages, feed-composition rules, roadmap phases, decided constraints).
`episteme-architecture.OLD.md` is a superseded v1 kept for reference only.

## Current state (update this section as phases land)

- **Phase 1 (ingestion + raw feed UI) and Phase 2 (LLM writer pipeline) are built.**
- Phase 2 has NOT had a live LLM end-to-end run yet: llama-server was offline during
  development (gateway + pipeline are unit-tested against mocked transports).
- **Blocker for the first live pipeline run:** no GGUF embedding model exists in
  `C:\selfhosting\models` (the Qwen3-Embedding-4B there is safetensors, unusable by
  llama-server). Set `LLM_MODEL_EMBED` in `.env` once one is present.
- The user launches llama-server via `C:\selfhosting\llama-cpp\launch-llama.ps1 server`
  (router mode, `--models-dir C:\selfhosting\models`, port 5001). Containers reach it at
  `host.docker.internal:5001`. Hardware: RTX 3080 10GB, 96GB RAM.
- Next up per roadmap: live Phase 2 verification, then Phase 3 (feedback + recommendation).

## Commands

```sh
docker compose up --build -d          # full stack: db (pgvector), migrate (one-shot), web, worker
curl http://127.0.0.1:8200/health     # web app: http://127.0.0.1:8200

# Trigger jobs immediately (otherwise: ingestion cron */30, pipeline cron 03:00)
docker compose exec worker procrastinate --app=episteme.worker.app.app defer episteme.ingest_all '{}'
docker compose exec worker procrastinate --app=episteme.worker.app.app defer episteme.run_pipeline '{}'

# Database
docker compose exec -T db psql -U episteme -d episteme

# Dev (venv at .venv, Windows)
.venv/Scripts/python -m pytest -q                        # all tests
.venv/Scripts/python -m pytest tests/test_llm.py -k retry  # single test
.venv/Scripts/python -m ruff check src tests
.venv/Scripts/python -m pip install -e ".[dev]"
```

## Architecture (the parts that span multiple files)

- **Everything is a plugin surface** (spec §11). Source adapters implement the
  `SourceAdapter` protocol (`ingest/base.py`), register via `@register`
  (`ingest/registry.py`), and are activated by a `Source` DB row with matching
  `type_name`. New pipeline stages/section types follow the same pattern of small
  interfaces + registration.
- **LLM access goes only through `llm/gateway.py`.** Code asks for a *role*
  (`writer`/`fast`/`embed`); config maps roles to model names (`LLM_MODEL_*` env).
  Structured output is double-enforced: JSON schema sent as `response_format`
  (llama.cpp grammar constraint) + pydantic validation with repair-prompt retries
  (`llm/schemas.py` is the contract). Embeddings are truncated to `EMBEDDING_DIM` (768)
  and re-normalized so any ≥768-dim embedding model works without schema changes.
- **Pipeline** (`worker/pipeline.py`): embed → cluster (pgvector cosine, 5-day window,
  incremental centroids) → triage (fast model: write/aggregate/skip per story) → write
  (hierarchical: long sources condensed by fast model first). Stages are plain async
  functions wrapped in procrastinate tasks; the orchestrator runs them role-batched so
  each model loads once per run. **The article `sources` section is always built from
  the DB, never by the LLM** — citations must not be able to hallucinate.
- **Jobs**: procrastinate (Postgres-backed queue, no broker). Worker and web are the
  same image with different entrypoints. Periodic tasks via `@app.periodic(cron=...)`,
  crons configurable through settings (`config.py` reads env / `.env`).
- **Schema management**: `bootstrap.py` (compose `migrate` one-shot service) does
  create_all + `ADDITIVE_MIGRATIONS` (idempotent DDL list for columns added to existing
  tables — create_all only creates missing tables) + procrastinate schema (guarded;
  `procrastinate schema --apply` is NOT idempotent). Switch to Alembic when churn grows.
- **Politeness toward sources is a hard requirement** (spec §5). All source HTTP goes
  through `ingest/http.py`: one global throttle (min 2s gap, ~N(3s,1s)) enforced via an
  httpx request event hook; conditional GETs (ETag/Last-Modified stored on `Source`);
  429 → persisted per-source `cooldown_until` honoring Retry-After. New adapters MUST
  use `polite_client()`. Never `docker compose down -v` casually — re-ingesting
  re-fetches every article from every source.
- **Two-tier feed** (spec §8): generated articles first, "you're caught up" divider,
  then infinite-scroll aggregation stream (htmx `revealed` sentinels swap in
  `/partials/*` pages). Falls back to raw source items until the pipeline has output.
  Frontend is server-rendered Jinja2 + htmx + vanilla CSS — no SPA framework, htmx
  until it demonstrably fails (user decision).

## Constraints decided with the user (do not silently revisit)

- Single-user forever: no auth, no user/profile columns.
- Always dark mode: true-black OLED theme, full-width grid (4K screen). No light theme.
- Media is hotlinked, never cached locally (storage concern); `image` rendering must
  degrade gracefully via `onerror` (remove banner) since link rot is accepted.
- Fully standalone: no integration with the user's other stacks (e.g., Odysseus).
- Jinja gotcha that already bit once: `dict.items` in a template resolves to the dict
  *method*; use `dict["items"]` subscript for the `items` key.
