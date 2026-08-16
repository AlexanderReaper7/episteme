# CLAUDE.md

Episteme: a self-hosted, single-user, LLM-powered personalized newsfeed. Sources are ingested continuously; a local LLM (llama-server on the host, port 5001) processes them overnight into generated articles plus a Google-News-style aggregation stream.

This file is rules and navigation only. The other three:

- [episteme-architecture.md](episteme-architecture.md) is the spec: data model, pipeline stages, feed-composition rules, roadmap.
- [docs/decisions/](docs/decisions/README.md) is why anything is the way it is, and what it was measured against. **`(0017)` below means `docs/decisions/0017-*.md`.** Grep it before changing something that looks arbitrary; add to it when we decide something new.
- [CLAUDE-TODO.md](CLAUDE-TODO.md) is what is built but not yet watched running. Mine to maintain. `TODO.md` is the user's, do not write to it.

## Current state

Phases 1 to 4 are built and live. "Live" and "tested" are different claims:

| area | state |
|---|---|
| ingestion, raw feed UI (1) | live |
| writer pipeline (2) | live-verified 2026-07-17, full run end to end |
| agentic writer, vision QA (2.5) | live |
| rich content sections (3) | write path live-verified 2026-07-19 |
| recommendation, feed ranking (4) | live since 2026-07-30 |
| host control agent, resource governor | live-verified 2026-08-01 |
| assistant rail, reader-requested articles (5) | built 2026-08-11, NOT yet watched running |
| benchmarks (`quick`) | live-verified 2026-08-15; `ladder`/`longctx`/`sweep` built, not watched |

**Read [CLAUDE-TODO.md](CLAUDE-TODO.md) before claiming a path works.** It holds the paths that were rewritten but not yet observed running, and the open content-quality gaps.

Operational facts with no other home:

- `EMBEDDING_DIM` is **1024**: the 4B embedder's 2560 truncated and re-normalized by the gateway. `cluster_similarity_threshold` (0.82) is tuned against truncated vectors, so re-tune it if the dimension changes (0004). Models: `Octen-Embedding-4B.Q8_0` (default `embed` role), `Octen-Embedding-0.6B.f16` (faster).
- llama-server comes from `C:\selfhosting\llama-cpp\launch-llama-v2.ps1 server`, started either by the user or by the host agent, which shells out to the same script with `-Detached` (0023). It brings up **two** processes: the router on 5001 (main and fast, swapped on demand) and a dedicated always-resident embed server on 5002. Containers reach both at `host.docker.internal`. Per-model flags, MTP, KV quant, `--embeddings`, `mmproj` live in `models-preset.ini` beside the launcher, in sections keyed by GGUF name minus extension. `launch-llama.ps1` is the superseded v1.
- At most one decode model in VRAM, and embed is a **separate server** rather than a third model in the router, because `--models-max` counts models globally with no per-model exemption: capping it at 1, which is what keeps main and fast from ever sharing VRAM, would evict the embedder. The embed model is CPU-only (`--n-gpu-layers 0`). Both are enforced host-side, not in this repo. `LLM_EMBED_BASE_URL=""` serves embeds from the router again.
- **Qwopus is a reasoner**: a 60-token cap returns EMPTY content with the whole budget spent on `reasoning_content`. Give vision calls room before concluding vision is broken (0009).
- Postgres is published to the host at `127.0.0.1:5433` (5432 was taken) for pgAdmin. Credentials in `.env`.

## Commands

```sh
# `POST /api/x` means `curl -X POST "$E/api/x"`. Set $E once:
E=http://127.0.0.1:8200               # PowerShell: $E = "http://127.0.0.1:8200"
docker compose up --build -d          # db (pgvector), migrate (one-shot), web, worker
GET    /health  # /admin is the dashboard, single-user, no auth

# ONLY `web` bind-mounts ./src. The WORKER runs the code baked into its image, so
# `docker compose restart worker` re-runs the OLD code. Any change under
# src/episteme/{worker,recommend,llm,ingest,research}/ needs a rebuild, and the
# check comes AFTER it (2026-07-30: two 10-minute runs "verified" a fix the worker
# did not have). Import the MODULE below: `from episteme.llm import gateway` gives
# the singleton re-exported by llm/__init__, so hasattr on it is a false negative.
docker compose build worker && docker compose up -d worker
docker compose exec -T worker python -c "import importlib; print(hasattr(importlib.import_module('episteme.llm.gateway'), '_as_llm_error'))"

# Jobs (ingest cron */30, pipeline 03:00). Stages are data-driven: each picks up
# whatever rows are unprocessed, so write runs without re-triaging.
POST   /api/jobs/defer/ingest_all  # or run_pipeline
POST   /api/jobs/defer/write?limit=2  # embed|cluster|triage|write|qa
POST   /api/jobs/defer/write?story_id=284  # rewrite, archives old post
POST   /api/jobs/defer/qa?post_id=8  # re-review even if scored
POST   /api/jobs/defer/ingest_source?source_id=4

# Topics bootstrap in two phases; read /admin/topics between them (0018).
POST   /api/jobs/defer/propose_topics
GET    /api/topics/proposal
POST   /api/jobs/defer/apply_topics
# Topics created while the embed endpoint was down have no vector, so nothing can
# ever fold into them. `embed` heals them automatically; this forces it.
POST   /api/jobs/defer/backfill_topic_embeddings

# Feedback. set_topic is ABSOLUTE (replaces the accumulated weight, 0 forgets);
# unblock_keyword is a counter-event, so it also lifts an NL-imposed block (0021).
POST   /api/feedback?kind=like&post_id=247
POST   /api/feedback/nl?text=more+deep-sea+biology
POST   /api/feedback?kind=set_topic&topic=astronomy&weight=4
POST   /api/feedback?kind=block_keyword&keyword=crypto
POST   /api/feedback?kind=unblock_keyword&keyword=crypto
DELETE /api/feedback/12  # exact undo + replay
GET    /api/profile  # /tune is the HTML view
POST   /api/profile/rebuild  # after retuning weights
POST   /api/jobs/defer/score  # rescore the whole feed; one pass per window (0022)

# Job history tiers (job_history_*_days): maintenance + scheduler 2d, ingest 7d,
# work 90d, ANY failure 90d. Nightly 04:30 (0032). /admin/jobs' default chip
# "Recent" applies NO filter: a 200-job window folded by admin.group_jobs.
POST   /api/jobs/defer/prune_job_history
GET    /api/jobs?limit=200
GET    /api/jobs?exclude_plumbing=true&limit=20
GET    /api/jobs?status=failed&class=work&limit=20 # scheduler|maintenance|ingest|work

# Pause persists a flag; the worker stops at the next unit boundary and unloads
# the decode models. Resume defers a pipeline run unless ?run=false. A pause
# records its author; the governor may only clear its own (0024).
POST   /api/pipeline/pause
POST   /api/pipeline/resume
# 409 while an interactive chat turn holds the lease; ?force=true to insist. Every
# AUTOMATIC unload goes through control.unload_unless_interactive instead (0037).
POST   /api/llm/unload  # free VRAM now, no pause

# The assistant rail (0036, 0037, 0038). HTML routes, no /api prefix; the turn
# itself is text/event-stream. One conversation at a time, id in app_state.
GET    /chat/panel  # the rail; base.html fetches it on load, outside <main>
POST   /chat/new
POST   /chat/turn  # form field `text`, ?post_id= for "the article I am reading"
POST   /chat/proposal/{id}/resolve?approve=true  # the ONLY path to a write handler
GET    /api/posts/search?q=deep-sea+biology&limit=5  # semantic + literal, merged

# llama.cpp lifecycle, logs and GPU sensing via the host agent (0023). It runs on
# the HOST, not in compose; everything here 503s cleanly when it is absent. Set
# LLM_HOST_AGENT_URL=http://host.docker.internal:5003 in .env. It has a tray icon
# and one console window, born hidden, shown from the tray (0042). Its deps live
# in llama_agent.py's PEP-723 header; console.py must NOT grow one of its own.
#   pwsh hostagent/install-task.ps1   # scheduled task at logon (-Remove, -Force)
#   uv run hostagent/llama_agent.py   # or a console; log: llama-cpp/logs/agent.log
#   ./hostagent/run-agent.ps1 -Show   # watch startup; -Spawn is the task's path
# The tray icon IS the host agent mark, and doubles as the status light: nodes are
# the decode server, edges the embedder, grey is down (0042, graphics/logo/README).
#   ./hostagent/open-agent.ps1        # start it, or show the running one; one door
#   pwsh hostagent/install-shortcut.ps1  # a .lnk for that (-Desktop, -Remove)
# Its window makes claims pytest cannot reach, so hostagent/tools/ holds the
# instruments that check them live: capture the window, the taskbar button, the
# open tray menu, read back what Textual actually painted. Read its README before
# writing another one - it is also where the win32 traps are written down (DPI
# virtualization, PrintWindow's blind spot, pystray's cached HMENU).
GET    /api/llm/backend  # ports, PIDs, uptime
POST   /api/llm/backend/start  # idempotent (locked)
POST   /api/llm/backend/restart
# stop is graceful: pauses, waits for the work unit, THEN kills. stopped:false if
# it outlasts llm_graceful_stop_seconds. Nothing dies mid-generation unforced.
POST   /api/llm/backend/stop  # ?force=true
GET    /api/llm/logs?which=router&tail=200  # or which=embed
GET    /api/llm/logs?which=router&since=40960  # delta (0034)
GET    /api/llm/logs/stream?which=router  # what the pane uses (SSE; curl needs -N)
GET    /api/llm/resources  # ~3.5s: per-process GPU, VRAM, games
docker compose exec worker python -c "import asyncio; from episteme.worker.governor import govern_resources; print(asyncio.run(govern_resources())['reason'])"

# Benchmarks (0039, 0040, 0041; docs/benchmarks/plan.md). HTML routes, no /api.
# A fixture is a REAL conversation replayed from llm_calls: a synthetic 4k prompt
# reports 3-4x the prefill a 19k writer call gets, so only `quick` may be synthetic.
POST   /admin/benchmarks/fixtures/capture  # name, stage, percentile (1.0 = largest)
POST   /admin/benchmarks/run  # scenario=quick|longctx|ladder|sweep, models, reps, predict
POST   /admin/benchmarks/{id}/cancel  # a flag on the row, read between stream chunks
GET    /admin/benchmarks/{id}/progress  # SSE; worker writes run.progress, web polls it
# sweep restarts llama-server with different flags, so it 422s without the host agent.

GET    /api/status  # /api/{status,sources,runs,jobs,stories,posts,llm-calls}
GET    /api/llm-calls?story_id=284&full=true  # full prompts/responses
# /post/{id}/provenance is every LLM call behind THIS post version. Retention pins
# exempt rows from the prune (?value=false unpins); the attempt pin covers a failed
# attempt, whose calls have post_id NULL (0012).
POST   /api/posts/20/pin
POST   /api/llm-calls/pin?attempt_id=<uuid>

# Narration (Fish Audio TTS, 0035). tts_enabled=false, so the nightly batch is off;
# on-demand streaming works regardless. FISH_API_KEY in .env. Voices are DB rows.
POST   /api/posts/247/narrate  # worker batch
GET    /api/posts/247/audio/stream?voice=  # web, on-demand
docker compose exec -T db psql -U episteme -d episteme -c 'select id,label,sort_order,enabled from voices order by sort_order;'

docker compose exec -T db psql -U episteme -d episteme
POST   /api/jobs/defer/backup_database  # pg_dump -Fc zstd to BACKUP_DIR

# Migrations: see the review rule below, which is where the workflow lives.
uv run python -m episteme.migrations status
docker compose run --rm migrate         # bootstrap: adopt-or-upgrade + seed

uv sync                                 # .venv from uv.lock; `uv run` auto-syncs first
uv run pytest -q
uv run pytest tests/test_llm.py -k retry
uv run ruff check src tests
uv lock                                 # re-resolve after editing dependencies

# _icon_sprite.html is GENERATED. Edit the glyph list in the script, never the
# sprite; it downloads the pinned @carbon/icons tarball itself (0029).
uv run tools/build_icon_sprite.py

# So are both logo SVGs: a port of the canvas prototype, which stays the authority
# on the geometry. graphics/logo/README.md is the spec for what the marks MEAN.
# The Episteme mark is generated into web/static/logo/, NOT into graphics/ - the
# image copies src only. It is the tab icon (base.html) and nothing else: still
# WIP, so the header is the wordmark alone.
uv run graphics/logo/build_svg.py
# The .ico is NOT converted from the SVG - it re-renders console.py's icon_image,
# so the tray, the taskbar and the shortcut are one picture by construction.
uv run tools/build_hostagent_ico.py
```

## Architecture (the parts that span multiple files)

Names to navigate by. The reasoning is in the numbered decision.

- **Everything is a plugin surface** (spec §11, 0002): `ingest/registry.py`, `recommend/scorers.py`.
- **LLM access goes only through `llm/gateway.py`** (0003). Ask for a role (`main`/`fast`/`embed`/`chat`), never a model or a URL. **`LLMError` is its whole error contract**, because every caller degrades against `except LLMError`. Schemas in `llm/schemas.py`. `chat_stream` is `chat_messages` streamed, terminating in one `{"type": "message"}` of identical shape (0038).
- **Pipeline** (`worker/pipeline.py`): embed → cluster (pgvector cosine, 5-day window, incremental centroids) → triage (fast model) → write (0008) → qa (`worker/qa.py`, 0026). Async functions in procrastinate tasks, role-batched by the orchestrator so each model loads once per run. **`sources` and `further_reading` are built in code, never from LLM text** (0007).
- **Sections**: nine-member typed union in `llm/schemas.py`, media closed-set (0013, 0027). One branch per type in `_sections.html`.
- **Recommendation** (`recommend/`, spec §8). **The `feedback` table is canonical, everything else derived** (0017). Vocabulary in `topics` (0018, 0019). Affinity stored, freshness in the query (0020). Topic chips inherit the rating's direction (0030). `blocks.py` defines a hard block once.
- **Observability** (`llm/observe.py`): every gateway call persisted to `llm_calls`, so one choke point covers even JSON-repair retries. Stage and story tagged by contextvar, never in gateway signatures. Deltas 0010, post scoping 0012.
- **The assistant** (`llm/chat.py` + `llm/chat_tools.py`, `web/chat.py`, 0036). **A `writes=True` tool is NEVER executed from `dispatch`** — it raises `WriteProposed`, and `execute_approved` is the only other path to a handler. Do not add a "just this once" branch; the gate is structural because a per-handler check is a convention that fails silently. Tool table + `validate_registry` in `chat_tools.py`, `tests/test_chat_tools.py` parametrizes the property over the registry.
- **One tool loop** (`agent.run_tool_loop`), shared by writer, QA and chat. Chat enters through the injected `turn` seam (0038); a second loop is the thing that is not wanted. Reader-requested stories: `Story.origin == "user"`, first in the queue, no `demote_story`, no thin-gate (0037). Interactive lease in `worker/control.py`: every AUTOMATIC unload goes through `unload_unless_interactive`.
- **Narration** (`src/episteme/tts/`, 0035). `script.build_script` is the seam an LLM preprocessing pass replaces.
- **Benchmarks** (`src/episteme/bench/`, 0039): a peer of `llm/`, never a caller. `BenchError` is its whole error contract and streaming is the only path, because prefill curves and cancellation both need the stream. **Never store or plot llama-server's aggregate rates** (0040): `prompt_progress` counters are CUMULATIVE, so `series.py` differences them, and `report.py` aggregates totals-over-totals, never means-of-ratios. `bench_run(run_id)` takes one int: the row is the parameter record. The host agent applies preset edits it is handed and chooses nothing (0041).
- **Jobs**: procrastinate on Postgres, no broker. Worker and web are one image, two entrypoints; crons in settings. Stalls 0016, retention 0032.
- **Schema is Alembic**, migrations inside the package so `COPY src ./src` ships them. **No `create_all` anywhere**, deliberately (0014). `bootstrap.py` adopts-or-upgrades, applies the procrastinate schema guarded (`schema --apply` is NOT idempotent), then seeds sources. Backup 0015.
- **Politeness is a hard requirement** (spec §5, 0005, 0006). All source HTTP through `ingest/http.py:polite_get`, which new adapters MUST use. **Never `docker compose down -v` casually**: re-ingesting re-fetches every article from every source.
- **Feed**: one ranked stream, `web/app.py:_rank_expr`, features and aggregate cards interleaved as equal units. The spec's divider and diversity quotas are deferred, so don't hunt for them. Infinite scroll is htmx `revealed` sentinels into `/partials/*`. **All feed content is a post** (0011).
- **Rendering and caching**: `templating.fragment_block(request, template)` decides what a response is, and every ETag derives from it (0031). Polling fragments 204 on an unchanged digest (0033). Icons only through `ico.icon` / `ico.toggle` (0029).

## Migrations: the review rule (user feedback, hard)

**Never delete a migration's `UNREVIEWED = True` line without having read the whole file in this session.** That deletion is the approval, and it is the only thing standing between an autogenerated draft and the database.

Reviewing means checking, at minimum:

1. **Renames.** `--autogenerate` cannot see them: it emits `drop_column` + `add_column`, which deletes the column's data. If a drop+add pair on one table is really a rename, replace both with `op.alter_column("t", "old", new_column_name="new")`. `new` flags this pattern as `possible_rename` in comments directly above the marker.
2. **Type changes.** The generated cast runs against existing rows and can truncate or fail.
3. **What the diff cannot see at all**: data backfills, values for existing rows under a new NOT NULL column, `CREATE EXTENSION`, anything in a JSONB payload.

**`upgrade` runs in the container**, because it takes a pg_dump first and pg_dump plus the `/backups` mount exist in the image, not on the host:

```sh
uv run python -m episteme.migrations status            # what exists, what is unreviewed
uv run python -m episteme.migrations new -m "add x"    # autogenerate a DRAFT from models.py
uv run python -m episteme.migrations new --empty -m "backfill y"   # data migration, no diff
uv run python -m episteme.migrations check <rev>       # re-run the hazard analysis
# ... read the file, correct it, delete its UNREVIEWED line ...
docker compose run --rm migrate                        # or just `docker compose up`
```

Running `upgrade` from the host stops with that container command rather than migrating unprotected, and that refusal is the whole point: what happened before 2026-08-02 is real data migrated with the safety net sitting in a file that entry point didn't touch (0015). Generating a revision needs a reachable database but applies nothing; `--empty` needs no diff. `tests/test_migrations.py` covers the gate, and `test_shipped_revisions_are_reviewed` fails the suite if an unreviewed revision is ever committed.

## Engineering principles (user feedback, hard)

- **Fix root causes, not symptoms.** If downstream code has to compensate for how data is produced or stored, deduplicating on render, filtering on read, patching on display, then the producer or storage layer is wrong; fix it there. A consumer-side workaround is acceptable only as an explicitly temporary bridge, agreed with the user, never silently shipped as the fix. *(Origin 2026-07-17: transcripts deduplicated at render time; the fix was delta storage at the source, 0010.)*
- **Don't self-authorize known design debt.** Noticing a smell and filing it under "acceptable for now" is the user's decision, not Claude's. Surface the smell and the proper fix; let the user choose.
- **When data is derivable, store the canonical minimum** and derive the rest in code. Redundant copies drift and bloat; derivation is testable.
- **Build the simplest mechanism that satisfies the stated requirement.** *(Origin 2026-07-20: asked for a gate on unreviewed migrations, I designed a hash-bound ledger and a two-step confirm command. The user wanted a line you delete by hand, and was right, 0014.)*
- **Cap test runs at 1-3 items.** A full pipeline stage is tens of minutes of GPU time, and a bad prompt is as visible in 2 articles as in 40. Every stage takes `?limit=`, most take `?story_id=` or `?post_id=`.

## Constraints decided with the user (do not silently revisit)

- Single-user forever: no auth, no user/profile columns. The approval card is therefore the ONLY thing between a model and an effect (0036); nothing else is checking.
- A chat tool that reads settings exposes a **curated allowlist**, never `settings.model_dump()`. `.env` holds `FISH_API_KEY` and the database password.
- Always dark mode: true-black OLED theme, full-width grid (4K screen). No light theme.
- Media is hotlinked, never cached locally; `image` rendering must degrade via `onerror` (remove banner), since link rot is accepted.
- Zero manipulative mechanics (spec §1). Entertainment and education blended, "a good Reddit".
- Prompts carry guidance, not quotas. Asking for "3 to 5 sections" produces sections that exist to fill the count.
- Frontend is server-rendered Jinja2 + htmx + vanilla CSS. No SPA framework (0001).
- Jinja gotcha that already bit once: `dict.items` in a template resolves to the dict *method*; use `dict["items"]` subscript for the `items` key.
- Decisions go in [docs/decisions/](docs/decisions/README.md), not in this file. This file is loaded into every session, so it holds only what has to fire without being looked up.
