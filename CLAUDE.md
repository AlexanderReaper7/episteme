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
- **Phase 2 verified live end-to-end (2026-07-17)**: full run on real models —
  embed 323/323 → cluster 287 stories (25 multi-item) → triage 287 (178 aggregate /
  90 write / 19 skip) → write 10 (`max_writes_per_run` cap). Zero errors and zero
  JSON-repair retries; the DB-built `sources` sections matched DB rows exactly on
  every article (incl. a 3-source cluster). Feed + article pages render all section
  types. Known **content-quality** gaps found in that run (machinery is fine; these
  are prompt/gating issues for Phase 4 `verify` / `quality_score`):
  1. Triage approves `write` for stories with too little source text — a 518-char
     photo blurb became an article whose writer looped 3 prose sections verbatim.
     Consider gating `write` on extracted-text volume.
  2. 4/10 articles restate `summary` sentences verbatim as a `prose` section.
  3. Writer sometimes emits funding/DOI boilerplate as a prose section.
- Embedding models: `Octen-Embedding-4B.Q8_0` (default `embed` role),
  `Octen-Embedding-0.6B.f16` (faster alternative). `EMBEDDING_DIM` is **1024**
  (0.6B native; 4B's 2560 truncated + re-normalized by the gateway). Octen is NOT
  MRL-trained, but truncating the 4B to 1024 was measured to preserve the full-2560
  similarity structure well (pearson 0.976, 88% top-5 neighbour overlap) and still
  beats the 0.6B natively (0.900 / 72%) — so the truncated 4B is the better choice.
  Truncation shifts cosines by ~±0.03 (max ~0.10), so `cluster_similarity_threshold`
  (0.82) should be tuned against truncated embeddings.
- The user launches llama-server via `C:\selfhosting\llama-cpp\launch-llama-v2.ps1 server`
  (router mode, port 5001; per-model flags — MTP, KV quant, `--embeddings` — live in
  `C:\selfhosting\llama-cpp\models-preset.ini`, sections keyed by GGUF file name minus
  extension). Containers reach it at `host.docker.internal:5001`. Hardware: RTX 3080
  10GB, 96GB RAM. `launch-llama.ps1` is the superseded v1.
- **Source access policy (user, hard):** if a source fails or returns degraded
  content to the polite client, use any *non-destructive* means to make it work —
  non-destructive excludes spamming/high volume. TLS impersonation and (if needed)
  a headless browser are in scope. Hard lines: never raise request volume
  (throttle + cooldowns stay), and don't fetch robots.txt-disallowed paths — but
  read robots.txt's literal bytes, not a WebFetch summary.
- **Two HTTP transport modes** (`ingest/http.py:polite_get`), chosen per source via
  the `http_mode` config key: `polite` (default; honest httpx, truthful UA) and
  `impersonate` (curl_cffi presenting a real Chrome TLS/JA3 fingerprint;
  `impersonate_profile` in config). Impersonate is for publishers whose bot-detector
  fingerprints the TLS handshake and rejects honest clients regardless of UA. It
  changes only *how we look*, never volume. `worker/tasks.py` auto-escalates a
  source blocked (403/429) in `polite` to `impersonate`, retries once, and persists
  the working mode. Adapters get HTTP via `polite_get` returning a transport-agnostic
  `FetchResponse`/`FetchError` (not raw httpx) — `SourceAdapter.extract(item, source)`
  takes the source so it can honor its mode.
- **Phys.org** uses `http_mode=impersonate`: its own bot-detector (no cf-ray, not
  Cloudflare) TLS-fingerprints, so honest httpx 429s regardless of UA while curl_cffi
  gets HTTP 200 and the full feed; robots.txt (read literally) permits `/rss-feed/`.
  Mullvad (WireGuard tunnel present) is the exit-IP lever in reserve if IP-based
  blocking ever appears.
- Next up per roadmap: Phase 3 (feedback + recommendation). Phase 2 quality tuning
  (see gaps above) is worth folding in, since Phase 3's signal informs it.

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
  (`llm/schemas.py` is the contract). Embeddings are truncated to `EMBEDDING_DIM` (1024)
  and re-normalized so any ≥1024-dim embedding model works without schema changes.
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
  through `ingest/http.py:polite_get`: one global throttle (min 2s gap, ~N(3s,1s))
  applied before every request; conditional GETs (ETag/Last-Modified stored on
  `Source`); 429 → persisted per-source `cooldown_until` honoring Retry-After. New
  adapters MUST use `polite_get()` (returns a transport-agnostic `FetchResponse`;
  raises `FetchError` on >=400). See the HTTP-transport-modes note above for
  `http_mode`/impersonation. Never `docker compose down -v` casually — re-ingesting
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
- Persist Claude memories in-project under `.claude/memory/` (with its own `MEMORY.md`
  index), not the global per-user memory store — user preference.
