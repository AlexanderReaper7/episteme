# CLAUDE.md

Episteme: a self-hosted, single-user, LLM-powered personalized newsfeed. Sources are ingested continuously; a local LLM (llama-server on the host, port 5001) processes them overnight into generated articles plus a Google-News-style aggregation stream.

This file is rules and navigation only. The other four:

- [docs/architecture.md](docs/architecture.md) is the spec: data model, pipeline stages, feed-composition rules, roadmap.
- [GLOSSARY.md](GLOSSARY.md) is what every load-bearing word means. Renaming a concept means editing it in the same commit.
- [docs/decisions/](docs/decisions/README.md) is why anything is the way it is, and what it was measured against. **`(0017)` below means `docs/decisions/0017-*.md`.** Grep it before changing something that looks arbitrary; add to it when we decide something new.
- [docs/verification.md](docs/verification.md) is what is built but not yet watched running. The agent maintains it. `TODO.md` is the user's, do not write to it.

## Current state

Phases 1 to 4 are built and live. "Live" and "tested" are different claims:

| area | state |
|---|---|
| ingestion, raw feed UI (1) | live |
| writer pipeline (2) | live-verified 2026-07-17, full run end to end |
| agentic writer, vision QA (2.5) | live |
| rich content sections (3) | write path live-verified 2026-07-19 |
| recommendation, feed ranking (4) | live since 2026-07-30 |
| llama-warden, the pause it announces (0057) | split out of this repo 2026-09-13, part of InferMux since 2026-10-03 (0058). Live-verified the same day with a game on the card: the warden paused us, repeated at 300s moving `contended_at` and not `since`, was killed mid-pause and left us correctly paused with a frozen clock, and a restarted warden re-announced into it without re-authoring the pause. The warden-driven RESUME is still unwatched |
| assistant rail, reader-requested articles (5) | built 2026-08-11, NOT yet watched running |
| benchmarks (`quick`) | live-verified 2026-08-15; `ladder`/`longctx` built, not watched. `sweep` removed (0060) |
| correspondents, Glance, Matsedel (0046) | live-verified 2026-08-29: two scrapes, four kitchens, five filed posts; a re-read that carries no menu keeps what is stored |
| Matsedel reads until the week exists (0054) | live-verified 2026-09-05: week 36 repaired 2 of 4 -> 4 of 4 kitchens, 0 placeholders; the skip runs a whole week in 0.1s with no outbound request; all four live pages raise `NotPublishedYet` for next week. The daily CRON firing on its own is not yet watched |
| feed cards written by `summarize` (0050) | live-verified 2026-08-30: full backfill of 683 posts in 78 minutes, all 688 published posts carry a card |
| card layout: a third picture, measured clamp (0052); header burger (0053) | live-verified 2026-08-31 in Firefox at 2560x1400, 900x600 and 390x844 |
| notifications over ntfy (0056) | live-verified 2026-09-10: both jobs published to the real server under token auth, the menu's `ä å ö` survived the round trip, and both messages were seen on the phone's lock screen. Nobody has tapped one, and the home screen icon has never been on a phone |

**Read [docs/verification.md](docs/verification.md) before claiming a path works.** It holds the paths that were rewritten but not yet observed running, and the open content-quality gaps.

Operational facts with no other home:

- `EMBEDDING_DIM` is **1024**: the 4B embedder's 2560 truncated and re-normalized by the gateway. `cluster_similarity_threshold` (0.82) is tuned against truncated vectors, so re-tune it if the dimension changes (0004). Models: `Octen-Embedding-4B.Q8_0` (default `embed` role), `Octen-Embedding-0.6B.f16` (faster).
- The models are served by InferMux on the host, its own project and a systemd service: llama-swap's router with the warden inside, on 5001 (0058). It starts main and fast on demand, swapping them, and lists the always-resident CPU embedder as a peer named `embed` (0059). Containers reach it at `host.docker.internal`, with a key per process (0058). Per-model flags live in InferMux's configuration, not in this repo. Until 2026-10-03 this was a llama-server router started from a PowerShell launcher on Windows; the decision records before 0058 describe that setup.
- At most one decode model in VRAM, and embed is a **separate server** rather than a third model in the router, because `--models-max` counts models globally with no per-model exemption: capping it at 1, which is what keeps main and fast from ever sharing VRAM, would evict the embedder. The embed model is CPU-only (`--n-gpu-layers 0`). Both are enforced host-side, not in this repo. `LLM_EMBED_BASE_URL` is empty by default, so embeds go through InferMux like every other role (0059).
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
POST   /api/jobs/defer/write?limit=2  # embed|cluster|triage|write|qa|summarize
POST   /api/jobs/defer/write?story_id=284  # rewrite, archives old post
POST   /api/jobs/defer/qa?post_id=8  # re-review even if scored
# The feed card's text (0050). The ONLY author of posts.summary for the kinds
# declaring `summarized`; runs after qa so an article is summarized from the body
# QA left, not the writer's draft. A post is NOT in the feed until it has one, so
# after a migration or a triage-only run this is what makes the cards appear.
POST   /api/jobs/defer/summarize?limit=2  # or ?post_id=247
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
# records its author; llama-warden may only clear its own (0024).
POST   /api/pipeline/pause
POST   /api/pipeline/resume
# The warden's verdict, pushed on every transition and repeated every 300s until
# it lands (0057). worker/contention.py decides what stopping MEANS; the warden
# decided THAT we stop. 422 on an action that is neither word - a 500 would read
# as "retry" and it would retry the same unusable word forever.
POST   /api/pipeline/announce  # {"action":"pause"|"resume","reason":...}
# 409 while an interactive chat turn holds the lease; ?force=true to insist. Every
# AUTOMATIC unload goes through control.unload_unless_interactive instead (0037).
POST   /api/llm/unload  # free VRAM now, no pause

# Correspondents and Glance (0046). HTML routes, no /api prefix. `/c` is core's
# prefix; a plugin's router is mounted under it with the enabled flag as one
# Depends, and every plugin must declare GET /glance. No plugins ship yet.
GET    /glance  # one block per enabled correspondent, each an htmx fragment
GET    /c/<slug>  # the correspondent's own page; /c/<slug>/style.css is its CSS
docker compose exec -T db psql -U episteme -d episteme -c 'select slug,label,enabled from correspondents;'

# The assistant rail (0036, 0037, 0038). HTML routes, no /api prefix; the turn
# itself is text/event-stream. One conversation at a time, id in app_state.
GET    /chat/panel  # the rail; base.html fetches it on load, outside <main>
POST   /chat/new
POST   /chat/turn  # form field `text`, ?post_id= for "the article I am reading"
POST   /chat/proposal/{id}/resolve?approve=true  # the ONLY path to a write handler
GET    /api/posts/search?q=deep-sea+biology&limit=5  # semantic + literal, merged

# InferMux's warden (0060): loaded models, GPU sensing, and one unload. All 503
# cleanly with LLM_WARDEN_URL empty. llama.cpp's lifecycle, logs and model flags
# are InferMux's, in its own UI; do not rebuild any of them here.
GET    /api/llm/backend  # InferMux's /running: loaded models, state, ttl
# unload is graceful: pauses, waits for the work unit, THEN asks InferMux.
# unloaded:false if it outlasts llm_graceful_stop_seconds. Nothing is cut off
# mid-generation unforced. InferMux itself refuses during an interactive request.
POST   /api/llm/backend/unload  # ?force=true
GET    /api/llm/resources  # a FRESH measurement: per-process GPU, VRAM, culprits
# What the warden last decided, and the policy it decided with. Cheap - it is the
# warden's cached state. The bench gate reads its threshold from here rather than
# keeping a second copy of the number (0057). Any Episteme key will do.
curl -s -H "Authorization: Bearer $(cat /run/secrets/episteme)" http://127.0.0.1:5001/warden/verdict

# Benchmarks (0039, 0040, 0060; docs/benchmarks/plan.md). HTML routes, no /api.
# A fixture is a REAL conversation replayed from llm_calls: a synthetic 4k prompt
# reports 3-4x the prefill a 19k writer call gets, so only `quick` may be synthetic.
POST   /admin/benchmarks/fixtures/capture  # name, stage, percentile (1.0 = largest)
POST   /admin/benchmarks/run  # scenario=quick|longctx|ladder, models, reps, predict
POST   /admin/benchmarks/{id}/cancel  # a flag on the row, read between stream chunks
GET    /admin/benchmarks/{id}/progress  # SSE; worker writes run.progress, web polls it
# The gate refuses a contended card. Its games check reads `games_running`, which
# InferMux does not send, so today it decides on load alone, as the warden does (0060).

# Correspondents (0046, 0047). Matsedel is the only one: `/c/matsedel` is the
# week, `/c/matsedel/stats` its history, `/glance` the dashboard of blocks. The
# cron is 06:00 UTC every weekday, and a kitchen whose five weekdays are
# already stored is skipped, so a normal week is still one read each (0054).
POST   /api/jobs/defer/matsedel_scrape  # re-reads all four sites, then re-files
docker compose exec -T db psql -U episteme -d episteme -c 'select w.week_key,s.name,count(d.id) from matsedel_weeks w join sources s on s.id=w.source_id left join matsedel_dishes d on d.week_id=w.id group by 1,2 order by 2;'

# Notifications over ntfy (0056). Both are their own cron, NOT hooks on the work
# they report: the pipeline finishes when it finishes, and the Matsedel scrape
# does nothing after Monday (0054) while the menu it stored is served on five
# days. Empty NTFY_BASE_URL is the off switch; nothing is sent and nothing raises.
POST   /api/jobs/defer/send_digest      # 06:00 UTC = 08:00 local; reads the newest finished run
POST   /api/jobs/defer/matsedel_notify  # 09:00 UTC weekdays; sends today's lunch post, or nothing
# The home screen icon is a manifest, no service worker. The PNGs are GENERATED
# from web/static/logo/episteme.svg, which is itself generated - never hand-edit.
uv run tools/build_pwa_icons.py

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

# So is the logo SVG: a port of the canvas prototype, which stays the authority on
# the geometry. graphics/logo/README.md is the spec for what the mark MEANS. The
# sibling mark left with llama-warden and the generator was split, not copied
# (0057): this one renders `episteme` and contains no other mark's code. That
# sibling came back unused on 2026-10-08 with its own generator, when InferMux took
# a mark of its own: graphics/obelisk-net/README.md.
# The Episteme mark is generated into web/static/logo/, NOT into graphics/ - the
# image copies src only. It is the tab icon (base.html) and nothing else: still
# WIP, so the header is the wordmark alone.
uv run graphics/logo/build_svg.py
```

## Architecture (the parts that span multiple files)

Names to navigate by. The reasoning is in the numbered decision.

- **Everything is a plugin surface** (spec §11, 0002): `ingest/registry.py`, `recommend/scorers.py`.
- **LLM access goes only through `llm/gateway.py`** (0003). Ask for a role (`main`/`fast`/`embed`/`chat`), never a model or a URL. **`LLMError` is its whole error contract**, because every caller degrades against `except LLMError`. `LLMUnavailable` is its one subclass, raised only when the connect failed, and **no stage loop may swallow it** (0055): a per-item `continue` is how 36 posts went through a closed port in 0.9s and the run was recorded `succeeded`. `tests/test_endpoint_gone.py` reads every stage's AST, so this is checked, not remembered. Schemas in `llm/schemas.py`. `chat_stream` is `chat_messages` streamed, terminating in one `{"type": "message"}` of identical shape (0038).
- **Pipeline** (`worker/pipeline.py`): embed → cluster (pgvector cosine, 5-day window, incremental centroids) → triage (fast model) → write (0008) → qa (`worker/qa.py`, 0026) → summarize (0050). Async functions in procrastinate tasks, role-batched by the orchestrator so each model loads once per run. **`sources` and `further_reading` are built in code, never from LLM text** (0007). The writer gets each source item's text WHOLE: the fast-model condense pass that stood in front of it is gone, and `max_source_chars_per_*` are runaway guards, not a budget (0050).
- **One stage writes every feed card** (`pipeline.summarize_posts`, 0050). `posts.summary` is the card's whole text and `summarize` is its only author for kinds declaring `PostKind.summarized` — the writer is not asked for one and QA's `set_title` cannot reach it. Staleness is mechanical: a grown cluster moves `stories.last_item_at` past `summarized_at`, and `qa._flush` clears the STAMP, never the text, so a published post is never without a card. A replaced summary moves to `post_summaries`. **The feed shows no post without a summary** (`web/app.py:_visible_now`), which is what makes the card contract true rather than aspirational.
- **Sections**: nine-member typed union in `llm/schemas.py`, media closed-set (0013, 0027). One branch per type in `_sections.html`.
- **Recommendation** (`recommend/`, spec §8). **The `feedback` table is canonical, everything else derived** (0017). Vocabulary in `topics` (0018, 0019). Affinity stored, freshness in the query (0020). Topic chips inherit the rating's direction (0030). `blocks.py` defines a hard block once.
- **Observability** (`llm/observe.py`): every gateway call persisted to `llm_calls`, so one choke point covers even JSON-repair retries. Stage and story tagged by contextvar, never in gateway signatures. Deltas 0010, post scoping 0012.
- **The assistant** (`llm/chat.py` + `llm/chat_tools.py`, `web/chat.py`, 0036). **A `writes=True` tool is NEVER executed from the loop** — `agent._dispatch` raises `WriteProposed` above every handler for EVERY harness, and `chat_tools.execute_approved` is the only other path to one. Do not add a "just this once" branch; the gate is structural because a per-handler check is a convention that fails silently. `tests/test_chat_tools.py` parametrizes the property over the whole registry, not the assistant's slice of it.
- **One tool loop** (`agent.run_tool_loop`), shared by writer, QA and chat. Chat enters through the injected `turn` seam (0038); a second loop is the thing that is not wanted. Reader-requested stories: `Story.origin == "user"`, first in the queue, no `demote_story`, no thin-gate (0037). Interactive lease in `worker/control.py`: every AUTOMATIC unload goes through `unload_unless_interactive`.
- **A harness is one agentic run** (`llm/harness.py`, 0049): prompt, tools, role, budgets, `on_idle`, `closing`, built per run and handed to the loop, so **the prompt and the tool list cannot be chosen separately**. A condition known at build time is a `{{slot}}` filled by `fill`; one that arrives mid-run is `harness.offer`, which adds the tool and returns the sentence announcing it. Tools live in ONE registry (`llm/tools.py`) and are selected by NAME, so a typo fails at build; `Tool.context` declares the least `ToolContext` a handler needs and `build` checks it. Budgets come off the context, never `settings`, which is how one `web_search` serves a writer allowed 6 and a chat turn allowed 2. Stage builders: `agent.writer_harness`, `qa.qa_harness`, `chat.chat_harness`.
- **Narration** (`src/episteme/tts/`, 0035). `script.build_script` is the seam an LLM preprocessing pass replaces.
- **A post stores its destination** (0047, built 2026-08-28): `posts.href` is where a card and `/post/{id}` both go, NULL means the post renders itself, and `ck_posts_body_iff_self_rendering` ties a body to rendering yourself. `models.POST_KINDS` records the decision per kind and `Post.kind` **refuses a value that is not in it**. The primary item is `models.PRIMARY_ITEM_ORDER`, one expression shared by `Story.items` and `pipeline._primary_item`.
- **Correspondents** (0046, GLOSSARY.md). A correspondent files finished posts, skipping triage and the writer, and owns a page under `/c/<slug>/` for standing content that never enters the feed. Plugins ship in-tree and run in-process, because only in-process code can be held to `polite_get`; external services keep their own database and are never copied into ours. **Filing is built** (`correspondents/filing.py`, 2026-08-28): one story per period, found through its items' hashes rather than a `period_key` column, upserted on a re-file. `posts.publish_at` is when a post becomes visible and `posts.expires_at` when it stops being, both NULL for no bound; the feed filters on both and sorts on the first, so a week of posts can come from one read and each leaves when it stops being true. **Registration and the routes are built too**: a `correspondents` row is the configuration, `correspondents/registry.py` resolves the slug to the plugin the way `get_adapter` resolves `Source.type_name`, and **core owns the `/c/<slug>/` prefix** (`web/correspondents.py`) so a plugin cannot declare a path that shadows a core route. The enabled flag is a `Depends` on the mount, not a check each view remembers. **Glance is built** (`web/glance.py`): core lays out one block per enabled correspondent and fetches each from that correspondent's own `GET /c/<slug>/glance`, required at registration so a missing block is a startup failure rather than a correspondent that looks down. A plugin's page goes through `templating.correspondent_page`, which renders `correspondent.html` and INCLUDES the plugin's template: inheritance breaks the boosted-fragment path, and importing the helper from `web/correspondents.py` is a circular import. **The filed card is built**: it names its correspondent by walking post -> story -> primary item -> `sources.type_name`, which IS the slug, and `filing._require_correspondent_source` is what keeps that true. **Matsedel is the first plugin** (`correspondents/matsedel/`): four restaurants' lunch menus, five weekday posts filed from them. Its reader works on the page's visible LINES, never on each site's markup, because a stale CSS selector yields an empty menu and an empty menu looks like a kitchen that posted nothing (0046). **`read_source` is told which week it is reading for** and raises `NotPublishedYet` for a page still showing an older one (0054): a stale page parses perfectly, and believing it lost two of four kitchens for a week. That is also the retry condition - the scrape runs every weekday and `store.kitchens_with_a_full_week` skips a kitchen whose five weekdays each carry a line `tag_of` does not call HIDDEN, so an ordinary week costs one read of each site and nothing after. **What a line IS comes from `tags.tag_of` and nowhere else** (label, dish, hidden): a hand-written `site -> exact line -> tag` table, falling back to `readers.is_label` for anything untagged. Tags are applied on read, so `matsedel_dishes` keeps what the restaurant wrote and a tag added today fixes every stored week. Its `tasks.py` is deliberately not imported by its `__init__.py`. `ingest_all` skips every source whose `type_name` is a correspondent slug (`worker/tasks.py:not_a_correspondents_source`), so the four rows keep their own `enabled` switch.
- **Notifications** (`notify.py`, 0056). One function, `publish`, and one rule: **a notification never fails the work it reports on**, so every error is caught and the return value says whether it landed. The **JSON publishing format**, not the header format, because HTTP headers are ASCII and a menu says `pepparsås`. The wording of the digest is a pure function (`worker/digest.py:compose`) over the run row and the cards it produced, and **what counts as new is `summarized_at`** (0050), which also keeps filed lunch posts out of it. The reader chose two events and declined alerts on pipeline and ingest failure, so do not add one; the digest reports a bad run once, in the morning. `notify.link` needs `public_base_url`, never `web_internal_url`.
- **Benchmarks** (`src/episteme/bench/`, 0039): a peer of `llm/`, never a caller. `BenchError` is its whole error contract and streaming is the only path, because prefill curves and cancellation both need the stream. **Never store or plot llama-server's aggregate rates** (0040): `prompt_progress` counters are CUMULATIVE, so `series.py` differences them, and `report.py` aggregates totals-over-totals, never means-of-ratios. `bench_run(run_id)` takes one int: the row is the parameter record. **The "GPU is busy" threshold is the warden's**, read off `/verdict` at a run boundary and never copied into settings (0057).
- **Who stops the pipeline for a game is not decided here** (0057). The warden (InferMux's since 0058, llama-warden before) measures the card, decides, and POSTs `{action, reason}` to `/api/pipeline/announce`; `worker/contention.py:apply_announcement` is the whole receiving end and decides only what stopping MEANS (pause, and hand VRAM back if nothing is mid-story). **Idempotency is a cross-process contract**, because the warden repeats its verdict every 300s until it lands: a repeat re-stamps `contended_at` and never `since` or `reason`. **Nothing expires** - a warden that dies while we are paused leaves us paused, and the only thing that makes that visible is `contended_at` having stopped advancing. `llm/warden.py` is the client (0060); `verdict()` is the cheap read, `resources()` a fresh measurement.
- **Jobs**: procrastinate on Postgres, no broker. Worker and web are one image, two entrypoints; crons in settings. Stalls 0016, retention 0032.
- **Schema is Alembic**, migrations inside the package so `COPY src ./src` ships them. **No `create_all` anywhere**, deliberately (0014). `bootstrap.py` adopts-or-upgrades, applies the procrastinate schema guarded (`schema --apply` is NOT idempotent), then seeds sources. Backup 0015.
- **Politeness is a hard requirement** (spec §5, 0005, 0006). All source HTTP through `ingest/http.py:polite_get`, which new adapters MUST use. The browser is the other way a source gets hit without anyone writing a request: `<body preload="mouseover">` prefetches boosted links, so an off-site card link carries `preload="none"` (`_feed.html`). **Not `hx-boost="false"`**, which reads like the fix and starts the request it prevents - the extension sends a boosted link through `htmx.ajax`, where `selfRequestsOnly` blocks a cross-origin URL, and an unboosted one through a raw XHR that nothing guards. **Never `docker compose down -v` casually**: re-ingesting re-fetches every article from every source.
- **Feed**: one ranked stream, `web/app.py:_rank_expr`, articles and aggregate cards interleaved as equal units. The spec's divider and diversity quotas are deferred, so don't hunt for them. Infinite scroll is htmx `revealed` sentinels into `/partials/*`. **All feed content is a post** (0011).
- **Rendering and caching**: **every page takes the window** (`--content-width: 100%`, 0051) - the article page included, chosen against readability with the numbers in hand, so do not reintroduce a centered measure. **A feed card is two thirds text, one third picture** (0052), stacked when the window is taller than it is wide, and **how much of the summary shows is measured per card** by `app.js:fitSnippet`, never a constant. **The header nav is one `<nav>`** that folds behind a burger below 40rem (0053) - a second copy for the narrow layout is the bug, not the implementation. `templating.fragment_block(request, template)` decides what a response is, and every ETag derives from it (0031). Polling fragments 204 on an unchanged digest (0033). Icons only through `ico.icon` / `ico.toggle` (0029). **Never style `:visited`** (0048): a link looks the same opened or not, and `tests/test_stylesheets.py` fails on any rule that singles it out, in a plugin's sheet as well as core's.
- **Failures render `web/errors.py` + `error.html`** (0043): HTML gets the full traceback, `/api` keeps FastAPI's JSON body untouched. **No debug/production toggle**, deliberately — single-user, no auth, tailnet only. A boosted navigation gets it as a fragment marked `HX-Error-Page`, which `app.js` swaps; nothing else is marked, so a failing 10s poll still leaves the page alone.

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

- Single-user forever: no READER auth, no user/profile columns, no `created_by`. The approval card is therefore the ONLY thing between a model and an effect (0036); nothing else is checking. **Amended 2026-08-28 (0046)**: an external correspondent authenticates with a per-service token. A service token is not a user and never becomes a column on a post.
- A chat tool that reads settings exposes a **curated allowlist**, never `settings.model_dump()`. `.env` holds `FISH_API_KEY` and the database password.
- Always dark mode: true-black OLED theme, full-width grid (4K screen). No light theme.
- Media is hotlinked, never cached locally; `image` rendering must degrade via `onerror` (remove banner), since link rot is accepted.
- Zero manipulative mechanics (spec §1). Entertainment and education blended, "a good Reddit".
- Prompts carry guidance, not quotas. Asking for "3 to 5 sections" produces sections that exist to fill the count.
- Frontend is server-rendered Jinja2 + htmx + vanilla CSS. No SPA framework (0001).
- Jinja gotcha that already bit once: `dict.items` in a template resolves to the dict *method*; use `dict["items"]` subscript for the `items` key.
- Decisions go in [docs/decisions/](docs/decisions/README.md), not in this file. This file is loaded into every session, so it holds only what has to fire without being looked up.
