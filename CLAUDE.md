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

- **Phases 1, 2, and 2.5 are built** (1: ingestion + raw feed UI; 2: LLM writer
  pipeline; 2.5: agentic writer + vision QA + renames — see the Phase 2.5 bullet
  below for what still needs live verification). **Phase 3 first push built
  (2026-07-19), not yet live-verified** — see the Phase 3 bullet below.
- **Phase 2 verified live end-to-end (2026-07-17)**: full run on real models —
  embed 323/323 → cluster 287 stories (25 multi-item) → triage 287 (178 aggregate /
  90 write / 19 skip) → write 10 (`max_writes_per_run` cap). Zero errors and zero
  JSON-repair retries; the DB-built `sources` sections matched DB rows exactly on
  every article (incl. a 3-source cluster). Feed + article pages render all section
  types. Known **content-quality** gaps found in that run (machinery is fine; the
  Phase 2.5 thin-gate, writer prompts, and qa stage now target exactly these —
  confirm on the next live run):
  1. Triage approves `write` for stories with too little source text — a 518-char
     photo blurb became an article whose writer looped 3 prose sections verbatim.
     Consider gating `write` on extracted-text volume.
  2. 4/10 articles restate `summary` sentences verbatim as a `prose` section.
  3. Writer sometimes emits funding/DOI boilerplate as a prose section.
- **Research agent verified live (2026-07-17)**: single-story acceptance run
  (JWST/MACS J0553.4-3342, story 284) — fast-model tool loop did 2 SearXNG searches
  and 5 page fetches (ESA Webb, arXiv, VENUS program site), persisted synthesized
  `research_notes` on the story, and the writer produced grounded prose using
  researched facts (no verbatim-summary restatement). `sources` section matched
  the DB row exactly; `further_reading` built from research fetches with
  already-cited URLs deduplicated. (Since 2026-07-18 the fetch log records final
  post-redirect URLs and the writer selects which fetched pages qualify —
  closed-set selection, see the architecture section.) Zero errors, article page renders.
- **Observability + admin + API verified live (2026-07-17)**: `llm_calls` /
  `pipeline_runs` tables, gateway instrumentation, `/api/*` JSON routes, and the
  `/admin` dashboard + per-story provenance page all live-tested — a story-284
  rewrite produced 5 tagged rows (4 research tool-chats + 1 writer call, correct
  stage/story_id/tokens/timing) rendered on the provenance page (then
  `/admin/story/284`; since 2026-07-18 it is `/post/{id}/provenance`). Postgres is
  published to the host at `127.0.0.1:5433` (5432 was taken) for pgAdmin;
  credentials in `.env`.
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
- **Phase 2.5 built (2026-07-17), not yet live-verified** (commits c9b52ff /
  d7ba9a2 / a296559): (a) *writer-led agentic write* — `llm/agent.py` is now a
  main-model tool loop per story (web_search / fetch_page / demote_story =
  editorial authority) that ends with the grammar-constrained `PostDraft` turn in
  the SAME conversation; fast keeps triage + a condense pass batched before any
  main-model work (no mid-story model swap, sources condensed exactly once);
  deterministic `min_write_chars` thin-gate kept as backstop. (b) *qa stage with
  vision* (`worker/qa.py`) — renders each unscored post via the real web app
  (`web_internal_url`), screenshots with headless Chromium (Playwright in the
  image), main model critiques against trusted sources with bounded revise rounds;
  sets `quality_score`, can demote; screenshots are stripped before `llm_calls`
  persistence. **Vision needs the user to enable `--mmproj` on Qwopus in
  models-preset.ini (stock Qwen3.6 mmproj, user-confirmed compatible) — until
  then qa fails per-post, non-fatally.** (c) *renames landed*: role
  `writer`→`main` (`LLM_MODEL_MAIN`), `articles`→`posts` + `kind`
  (**post**/**feature** nomenclature), `/post/{id}`, `/api/posts` —
  `bootstrap.RENAME_MIGRATIONS` runs before create_all. (d) prompts carry
  guidance, not quotas. (e) *model residency*: ≤1 decode model in VRAM, embed
  pinned to system RAM (`--n-gpu-layers 0`) — enforced host-side in the router
  config, not in this repo. Mission framing: entertainment + education blended,
  zero manipulative mechanics ("a good Reddit"). See spec §1/§7/§12.
- **Phase 3 first push built (2026-07-19), write path live-verified same day** —
  rich content sections. Scope decided with the user: media + rich sections now; micro-posts,
  minigames (definitions now in spec §12), quality gate + draft review UI, and
  OpenAlex tracing are later pushes. (a) `Section` union grew to 9 types:
  `image`/`video`/`quiz`/`chart` (vega-lite)/`diagram` (mermaid)/`timeline`/
  `glossary` + existing prose/key_points (`llm/schemas.py`; quiz validates
  answer_index in range). (b) *Media is closed-set like citations*: the writer
  seed lists ingested `media_refs` as "Available media" (exact URLs);
  `pipeline.sanitize_media_sections` drops non-candidate URLs and stamps
  `attribution`/`source_url` from the DB; QA revisions pass the same sanitizer
  (`qa.apply_revision`). No MediaAsset indirection — sections store the URL
  directly. (c) enrich stage folded into the writer (user decision 2026-07-19):
  rich sections come from the same agentic conversation, guidance not quotas;
  spec §7 stage 4 annotated. (d) RSS extraction now captures YouTube/Vimeo
  *embeds* (iframes, not mere links) as `kind="video"` media_refs. (e) Rendering:
  `_sections.html` partial (one branch per type), `video_embed` whitelist filter
  (youtube-nocookie/vimeo only), vendored vega/vega-lite/vega-embed/mermaid in
  `static/vendor/` (versions + re-download commands in its README) loaded only
  when the post contains chart/diagram, `static/post.js` hydrates
  quiz/chart/diagram with dark themes, failed renders collapse to the caption.
  QA screenshot settle bumped 500→1500ms for client-rendered sections.
  (f) *finish_research tool added during live test (2026-07-19)*: the writer's
  first live run ended research by calling `demote_story` ("no need to demote")
  — the only terminal-looking tool — killing the feature; `agent.py` now offers
  `finish_research(note)` as the explicit "done researching" affordance,
  `demote_story`'s description says it KILLS the feature, and the loop breaks to
  the draft on finish (regression test in test_agent.py).
  **Live-verified (2026-07-19, story 291 rewrite → post 247)**: grammar handled
  the 9-type union on Qwopus zero-retry; writer picked 1 of 3 offered images
  with a real caption (skipped quiz/chart for a news-y item — correct per
  guidance); sanitizer passed it and stamped `attribution`/`source_url` from the
  DB; `sources` (3 items) + `further_reading` (2 fetched URLs) DB/fetch-log
  built; page renders image figure + onerror, vendor JS correctly NOT loaded
  (no chart/diagram). Loop exit was the stuck-budget breaker (12 fetch attempts),
  so `finish_research` itself hasn't fired live yet. Still to verify live:
  chart/diagram/quiz/timeline/glossary emission on a suitable story, video
  sections (no video media_refs ingested yet), QA vision pass (needs `--mmproj`).
  Provenance quirk FIXED (2026-07-19): write-side calls now carry a
  per-generation `attempt_id` (uuid, stamped at call time via `llm_context`,
  column on `llm_calls`); `_stamp_post_calls` targets exactly that attempt, so
  a failed attempt's calls can never be swept into a later post's provenance —
  they stay attempt-tagged, post_id NULL, visible only in story-level history
  (`/api/stories/{id}/llm-calls`). The bootstrap timestamp-backfill is guarded
  with `attempt_id IS NULL` for the same reason. Post 247's data was repaired
  in place (job 1822's 7 calls unstamped, tagged `repair-job1822-...`).

## Commands

```sh
docker compose up --build -d          # full stack: db (pgvector), migrate (one-shot), web, worker
curl http://127.0.0.1:8200/health     # web app: http://127.0.0.1:8200

# Trigger jobs immediately (otherwise: ingestion cron */30, pipeline cron 03:00)
curl -X POST http://127.0.0.1:8200/api/jobs/defer/ingest_all      # or run_pipeline
# (equivalent: docker compose exec worker procrastinate --app=episteme.worker.app.app defer episteme.ingest_all '{}')

# Single pipeline stages (data-driven: each picks up whatever rows are unprocessed,
# so write runs without re-triaging, qa without writing). Optional caps/targets:
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/write?limit=2"        # embed|cluster|triage|write|qa
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/write?story_id=284"   # rewrite (archives old post)
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/qa?post_id=8"         # re-review even if scored
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/ingest_source?source_id=4"

# Pause/resume (resource governor lever): pause persists a flag in app_state; the
# worker stops at the next unit boundary (story/post/batch) and unloads the decode
# models from VRAM. Resume clears the flag and (by default) defers a pipeline run,
# which picks up exactly where the pause stopped.
curl -X POST http://127.0.0.1:8200/api/pipeline/pause
curl -X POST http://127.0.0.1:8200/api/pipeline/resume     # ?run=false to only clear
curl -X POST http://127.0.0.1:8200/api/llm/unload          # free VRAM now, no pause

# Admin dashboard + JSON API (single-user, no auth — decided constraint)
# http://127.0.0.1:8200/admin                 status, sources, pipeline runs, job queue, defer buttons
# http://127.0.0.1:8200/post/{id}/provenance  every LLM call behind THIS post version (post-scoped, not admin)
curl http://127.0.0.1:8200/api/status    # /api/{status,sources,runs,jobs,stories,posts,llm-calls}
curl "http://127.0.0.1:8200/api/llm-calls?story_id=284&full=true"  # full prompts/responses

# Retention pins (exempt from the auto-prune; ?value=false unpins). Post pin also
# has a button on the provenance page; attempt pin covers a failed attempt's
# calls (post_id NULL — attempt_id visible in /api/stories/{id}/llm-calls).
curl -X POST http://127.0.0.1:8200/api/posts/20/pin
curl -X POST "http://127.0.0.1:8200/api/llm-calls/pin?attempt_id=<uuid>"

# Database
docker compose exec -T db psql -U episteme -d episteme

# Backup: pg_dump -Fc --compress=zstd into BACKUP_DIR (host bind-mount, default
# ./backups), pruned past backup_retention_days. Worker-owned; no scheduled cron but is a one liner to add.
# Restore a dump with pg_restore.
curl -X POST http://127.0.0.1:8200/api/jobs/defer/backup_database

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
  (`main`/`fast`/`embed`); config maps roles to model names (`LLM_MODEL_*` env).
  Structured output is double-enforced: JSON schema sent as `response_format`
  (llama.cpp grammar constraint) + pydantic validation with repair-prompt retries
  (`llm/schemas.py` is the contract; `agent.request_validated` is the in-conversation
  variant). Embeddings are truncated to `EMBEDDING_DIM` (1024)
  and re-normalized so any ≥1024-dim embedding model works without schema changes.
- **Pipeline** (`worker/pipeline.py`): embed → cluster (pgvector cosine, 5-day window,
  incremental centroids) → triage (fast model: write/aggregate/skip per story) → write
  (main-model agentic loop per story — research tools + demote authority + final
  constrained draft in one conversation; fast condenses long sources in a batch
  beforehand) → qa (`worker/qa.py`: screenshot the rendered post, vision critique,
  bounded revise rounds, sets `quality_score`). Stages are plain async
  functions wrapped in procrastinate tasks; the orchestrator runs them role-batched so
  each model loads once per run. **The post `sources` / `further_reading` sections are
  always built from the DB / fetch log, never from LLM free text** — citations must
  not be able to hallucinate, and QA revisions can only replace body sections.
  `further_reading` is the writer's `further_reading_urls` selection intersected
  with the fetch log (closed set: the model contributes judgment about which fetched
  pages were relevant — dead-end fetches stay out — but only fetch-log membership
  puts a URL on the page; the log stores final post-redirect URLs).
- **Observability** (`llm/observe.py`): every gateway call is persisted to `llm_calls`
  (request/response, tokens, timing; embeds log batch size only) — the gateway is
  the single choke point, so instrumentation there covers everything including
  JSON-repair retries. The pipeline tags calls with stage + story via contextvars
  (`llm_context`), never by changing gateway signatures. Tool loops wrap themselves
  in `llm_conversation()`: their rows store message **deltas** chained by
  `chain_id`/`seq` (full transcript = concat of `request.messages` + `response` in
  seq order; a prefix hash detects rewritten history and starts a fresh chain
  rather than storing a broken delta). Reconstruction lives in `web/admin.py:_group_calls`. Each orchestrator pass writes
  a `pipeline_runs` row (per-stage counters, outcome). Recording is best-effort
  (failures swallowed, `llm_log_enabled` off in unit tests via conftest) and pruned
  after `llm_log_retention_days`. The `/api/*` JSON routes are the query layer; the
  `/admin` HTML pages (dashboard, queue) and the public `/post/{id}/provenance`
  page are thin views over the same functions. **Provenance is post-scoped**
  (2026-07-18): `llm_calls.post_id` records which post generation a call
  produced — qa stamps it at call time; condense/write calls run before the
  post row exists, so `pipeline._stamp_post_calls` stamps them right after it
  lands; triage stays `post_id NULL` (story-level, shown on every version's
  page). A rewrite archives the old post (`archived_at`) with its calls intact —
  each version's provenance page shows only its own calls, and the retention
  prune deletes archived posts (and, by age, their calls) past
  `llm_log_retention_days` — **unless pinned** (2026-07-19): `posts.pinned`
  keeps a version plus its complete provenance page (stamped calls + shared
  story-level calls, same `POST_SCOPED_STAGES` split as the API — the constant
  lives in `models.py` so prune and API can't drift); `llm_calls.pinned`
  (set per attempt via `POST /api/llm-calls/pin`) protects a failed attempt's
  unstamped calls, which no post pin can reach.
- **Jobs**: procrastinate (Postgres-backed queue, no broker). Worker and web are the
  same image with different entrypoints. Periodic tasks via `@app.periodic(cron=...)`,
  crons configurable through settings (`config.py` reads env / `.env`).
- **Schema management**: `bootstrap.py` (compose `migrate` one-shot service) does
  `RENAME_MIGRATIONS` (guarded table renames, BEFORE create_all so data isn't
  stranded under the old name) + create_all + `ADDITIVE_MIGRATIONS` (idempotent DDL
  list for columns added to existing tables — create_all only creates missing
  tables) + procrastinate schema (guarded; `procrastinate schema --apply` is NOT
  idempotent). Switch to Alembic when churn grows.
- **Politeness toward sources is a hard requirement** (spec §5). All source HTTP goes
  through `ingest/http.py:polite_get`: one global throttle (min 2s gap, ~N(3s,1s))
  applied before every request; conditional GETs (ETag/Last-Modified stored on
  `Source`); 429 → persisted per-source `cooldown_until` honoring Retry-After.
  Per-source config keys for touchy rate limiters (both set on Phys.org,
  2026-07-18, after repeated 429 cooldowns at global pacing):
  `min_request_gap_seconds` (extra per-HOST spacing on top of the global
  throttle — other hosts unaffected) and `fetch_interval_minutes` (scheduled
  `ingest_all` polls the source less often than the cron; manual
  `ingest_source` defers still force). New
  adapters MUST use `polite_get()` (returns a transport-agnostic `FetchResponse`;
  raises `FetchError` on >=400). See the HTTP-transport-modes note above for
  `http_mode`/impersonation. Never `docker compose down -v` casually — re-ingesting
  re-fetches every article from every source.
- **Two-tier feed** (spec §8): generated articles first, "you're caught up" divider,
  then infinite-scroll aggregation stream (htmx `revealed` sentinels swap in
  `/partials/*` pages). Falls back to raw source items until the pipeline has output.
  **All feed content is a post** (decided 2026-07-18): aggregate cluster cards are
  identity-only `Post` rows (`kind="aggregate"`, content columns NULL — the card
  renders from the story's items at read time). Triage mints the card on an
  aggregate verdict; a written feature archives it (at most one published post per
  story); every demote path (writer, thin-gate, QA) re-mints it via
  `pipeline.ensure_aggregate_post`. QA skips aggregates. This gives every visible
  content unit a `/post/{id}/provenance` page and, in Phase 4, a uniform feedback
  target.
  Frontend is server-rendered Jinja2 + htmx + vanilla CSS — no SPA framework, htmx
  until it demonstrably fails (user decision).

## Engineering principles (user feedback — hard)

- **Fix root causes, not symptoms.** If downstream code has to compensate for how
  data is produced or stored — deduplicating on render, filtering on read,
  patching on display — the producer/storage layer is wrong; fix it there. A
  consumer-side workaround is acceptable only as an explicitly temporary bridge,
  agreed with the user, never silently shipped as the fix.
  *Origin (2026-07-17): `llm_calls` stored the full growing transcript per tool
  turn and the provenance page deduplicated at render time; the right fix was
  delta storage (`chain_id`/`seq` + prefix-hash integrity) at the source.*
- **Don't self-authorize known design debt.** Noticing a design smell and filing
  it under "acceptable for now" in the docs is a decision the user makes, not
  Claude. Surface the smell and the proper fix; let the user choose.
- **When data is derivable, store the canonical minimum** and derive the rest in
  code (cf. sources sections built from the DB, transcripts from deltas).
  Redundant copies drift and bloat; derivation is testable.

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
