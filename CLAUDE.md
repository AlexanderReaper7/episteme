# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Episteme: a self-hosted, single-user, LLM-powered personalized science newsfeed. Sources are
ingested continuously; a local LLM (llama-server on the host, port 5001) processes them
overnight into generated articles plus a Google-News-style aggregation stream.

**Read [episteme-architecture.md](episteme-architecture.md) first** — it is the authoritative
spec (data model, pipeline stages, feed-composition rules, roadmap phases, decided constraints).

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
  the `articles`->`posts` rename is now folded into the Alembic baseline. (d) prompts carry
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

- **Phase 4 first push built AND live-verified (2026-07-29)** — the
  recommendation system. Four parts, each independently verifiable: (a) canonical
  **topic vocabulary** (`topics` table + `recommend/topics.py`), resolved in code
  from whatever triage/the writer emit, bootstrapped from existing free-text tags
  by a **two-phase** propose/apply job (`/admin/topics` renders the proposal for
  review — nothing is applied by the job that computes it); (b) **feedback log
  canonical, profile derived** — `interest_profile` is a cache rebuilt by
  replaying `feedback`, never mutated, so undo is exact and the half-life/weights
  can be retuned retroactively; each event carries its own embedding + topic
  snapshot so replay never depends on mutable (or pruned) rows; reader-facing at
  `/tune`, controls on every card and article; (c) **scoring** — seven `Scorer`
  implementations behind a registry, `Post.affinity_score` stored with a
  `score_components` breakdown, feed ordered by `tanh(affinity/scale) +
  age_in_tau_units` against a FIXED epoch (bounded and symmetric so soft signals
  reorder rather than bury, and pure-per-row so keyset pagination stays sound);
  (d) **write-side** — hard blocks filter (one predicate in `recommend/blocks.py`
  shared by the feed and the write queue), the write queue is ordered by triage
  quality plus a bounded affinity term, and triage/writer get a qualitative
  reader digest. Deferred by decision: implicit dwell/scroll signals, the "why am
  I seeing this" chip, diversity/serendipity quotas, OpenAlex authority.
  **Activated 2026-07-30** — migration `9ef644e53753` applied, vocabulary
  proposed/reviewed/applied, feed ranking live. (The activation sequence, for
  reference: migration → `propose_topics` → review `/admin/topics` →
  `apply_topics` → interest statement at `/tune`. Before it runs, the vocabulary
  grows organically per story and the feed ranks on freshness alone — NULL
  `affinity_score` reads as neutral.)
  **Live run (2026-07-29)**: migration applied via the compose `migrate`
  one-shot (pre-migration dump written first); feedback → replay → profile →
  score → reordered feed verified end-to-end in the browser (339 posts scored,
  668 rank inversions, max 19.3h — inside the 2·τ=72h bound the formula
  guarantees); undo verified exact through both the API and `/tune`. Three
  defects were found live and fixed, each with a regression test:
  1. *Keyword blocks emptied the feed of aggregates.* `NULL ILIKE …` is NULL,
     not false, so `~or_(exists, title.ilike, summary.ilike)` went NULL for
     every aggregate card (their content columns ARE NULL) and `WHERE` dropped
     it — one blocked keyword hid 278 of 339 posts, silently.
     `blocks.py` now wraps the post-level columns in `COALESCE(col, '')`.
  2. *Cluster naming misnamed 86% of the vocabulary.* Asked to name 734
     clusters in one call, the fast model held index alignment for 98, slipped
     one, and never recovered — the {quantum physics, quantum mechanics}
     cluster came back named "roman history". A wrong index is a well-formed
     response, so nothing caught it. `_name_clusters` now sends only
     MULTI-MEMBER clusters (a singleton's canonical name is its one member —
     and the singletons were 656 of the 734 that made the listing unmanageable),
     in batches of `NAMING_BATCH`, and rejects any name sharing no word with its
     cluster (`_plausible_name`), falling back to the most-frequent member.
  3. *NL feedback under-read hard refusals.* "I never want to see anything about
     crypto" produced a down-rank, not a block, and "keep it technical" set no
     difficulty. `FEEDBACK_INTENT_SYSTEM` now states the triggers as concretely
     as the cautions (a refusal is recorded as BOTH a block and a less-topic).
  **Manual profile editing (built + live-verified 2026-07-30)** — `/tune` is
  directly manipulable: drag the topic sliders, and add/remove hard blocks.
  1. *Weights are sliders committed by Save.* Dragging several is ONE editing
     session, so nothing posts until the reader says so: the whole list is one form
     (`POST /tune/weights`, fields `w:<slug>`), `app.js` paints the live readout +
     centre-anchored fill and tracks which sliders moved, and `revert` is purely
     local (no request). A dragged value is **absolute**: `set_topic` (kind added
     with `feedback.value`, migration `2813f1a15b8e`) REPLACES whatever the log had
     accumulated for that topic rather than adding a step; 0 forgets it. Each topic
     is still its own event (undo stays per-topic) but the batch is one transaction
     and ONE replay — `rebuild` is a full replay, so per-slider rebuilds would
     replay the log N times to reach the state the last one produces anyway.
     Two properties differ from every other signal, both deliberate: a set **does
     not decay** (the panel must still read 4.0 next month — a control whose value
     drifts on its own is a control that lies; same reasoning as `hide_source`), and
     it is an **anchor, not a lock** (later likes/steering still move the weight
     from there; only the history *before* the edit is discarded). `set_topic` is
     NOT in `feedback.TOPIC_KINDS` — that tuple drives the paired more/less button
     state on cards. A "set a topic by name" box (datalist of the whole vocabulary,
     free text still allowed via `topics.resolve`) is the only way to reach a topic
     with no slider yet.
     **Only moved sliders are recorded** (`web/feedback.py:changed_weights`): the
     browser posts all of them, and `dirty` (from app.js, which holds each slider's
     rendered starting value) says which changed. app.js's comparison must be
     NUMERIC — a browser sanitizes a range value onto its step grid and drops the
     trailing zero, so a slider rendered at `-2.0` reads back `"-2"`. *This bit live
     on the first load: string comparison marked four untouched whole-numbered
     sliders dirty before anyone touched them.* The slider list **requires JS** and
     says so in a `<noscript>` (2026-07-30, code review): there was a documented
     no-JS fallback comparing submitted values against the stored profile, but it
     was unreachable (the template always posts `dirty=""`) and it was also the
     wrong rule — stored weights decay past the slider's rounding on their own, so
     it would have recorded sliders nobody touched. An absent `dirty` now records
     nothing; the "set a topic by name" box works without JS and reaches every
     topic. Regression tests in test_feedback_render.py.
  2. *Hard blocks are add/removable.* `block_keyword` / `unblock_keyword` (new
     kinds; `feedback.keyword`, migration `bed89c48b376`) plus an outlet picker and
     an × on every chip. Unblocking a **keyword** is a counter-event, folded in time
     order, because a keyword block can come from a natural-language refusal —
     deleting that statement to lift the block would also delete the topic weights
     it set. Unblocking a **source** instead DELETES the `hide_source` events: a
     source block has no other origin, so that is an exact undo, and it keeps the
     per-post hide button truthful (it renders as pressed from the event's
     existence, so a counter-event would leave it claiming the outlet is hidden).
     `profile.normalize_keyword` is the single definition of a keyword's identity —
     if a block from a statement and an unblock from a chip normalized differently
     the × would silently do nothing.
     `feedback.keyword` is deliberately not the `topic` column: a keyword is matched
     literally against title/summary, a topic is slugified into the vocabulary, and
     one future mix-up there turns a down-rank into a content-removing block.
  *Live-verified 2026-07-30 in the browser* (real clicks/drags, not curl): clean
  dirty state on load; centre-click → 0.0 with fill/readout/Save reacting; two
  sliders dragged → `Saved 2 weights.`, exactly 2 events, the 4 untouched sliders
  unchanged and eventless, one coalesced rescore job; revert restored all six with
  zero requests; keyword block/unblock (removing the statement-imposed `crypto`
  block left the statement AND its −crypto weight intact); outlet blocked → chip +
  gone from the picker → unblocked → `hide_source` event deleted, offered again.
  **Rescore coalescing (user-approved, built 2026-07-30)** — every feedback write
  used to call `defer_rescore()` unconditionally, so N clicks enqueued N
  full-corpus rescores (8 observed in one session). `defer_rescore` now schedules
  the pass `rescore_debounce_seconds` out (default 20, `0` disables) under
  procrastinate's `queueing_lock`: its partial unique index (one `todo` row per
  lock) makes the DATABASE refuse the duplicate, so there is no application-side
  bookkeeping to drift. Leading-edge and fixed-width — the window starts at the
  first signal and is never extended, so continued clicking cannot starve the
  rescore — and the lock frees the moment a worker picks the job up, so a signal
  arriving mid-pass (which that pass may already have read past) correctly queues
  the next one. Nothing user-visible waits on it: `rebuild` has already committed
  the profile; only the stored `affinity_score` ordering lags, by at most the
  window. *Verified live 2026-07-30: 3 Like clicks in the browser → 3 feedback
  rows → exactly 1 score job; 3 undos → 1 more.*

  **Live topic-vocabulary run (2026-07-30)**: 860 raw labels → 749 canonical
  topics (82 multi-member clusters, 667 singletons), applied after a pre-apply
  `pg_dump`. Every story/post topic array rewritten, zero raw variants left in
  the DB or rendered in the feed; `quantum-physics` correctly absorbed
  `quantum-mechanics`/`quantum` (the cluster that the pre-fix run named "roman
  history"). Naming cost fell from 1 call / 570 s / 14 199 output tokens to
  4 calls / 24.7 s / 2050. **The 0.86 `topic_bootstrap_threshold` is right, not
  too strict** — an earlier guess that the weak collapse meant a bad threshold
  was wrong: the top singletons are `deep-sea biology`, `public health`,
  `archaeology`, `neuroscience`, `genetics`, `ecology`, `physics` — genuinely
  distinct fields, not near-duplicates. The distribution is Zipfian (484 of 749
  used exactly once; the top 80 cover 65.6% of all tag uses), which is why
  `topic_vocabulary_prompt_limit=80` is adequate. Lowering the threshold would
  start fusing distinct fields.

  **Code-review pass (2026-07-30, fixed + regression-tested, NOT yet live-verified)**
  — eight findings against the Phase 4 diff. The two structural ones first:
  1. *A topic's `slug` is now its permanent identity.* Weights were keyed by
     `slugify(label)` while `rename` moved the label and not the slug, so a rename
     discarded the topic's learned weight and left the row unreachable by
     `_by_slug` for its own new label (next emission → duplicate row). **Chosen
     with the user for forward compatibility with a related-labels graph** — the
     idea being finer-grained preferences where disliking "blue" also weakly
     dislikes "color" and "ocean". Edges need stable nodes: a key derived from the
     current label re-keys the node and dangles every edge on the first spelling
     correction. So: `slug` is assigned once and never moves; `rename` changes
     `label` only and adds `slugify(new_label)` to `aliases` so the new wording
     resolves back; `merge` (which genuinely ends an identity) repoints
     `feedback.topic` / `topics_snapshot` / `parsed_intent[].topic` at the
     survivor and the API route rebuilds + rescores after it. Feedback rows store
     SLUGS (`topics.resolve_slugs`); `Candidate.topic_slugs` replaced
     `Candidate.topics`, resolved via one `topics.slug_index` per pass;
     `profile.replay` keeps `slugify` as an idempotent NORMALIZER (a slug
     slugifies to itself), which is also why no data migration was needed — rows
     written before this land on exactly the key they always did.
  2. *`resolve` is not positionally aligned with its input* (it dedups and drops),
     and `record_nl` zipped against it — "less crypto, more quantum computing"
     could be recorded as its own inverse, into the canonical `parsed_intent`.
     `topics.resolve_entries` (raw label → row) is now the primitive; `resolve` /
     `resolve_slugs` are views of it.
  Then: `_as_llm_error` now also covers the response PARSE (a 200 that isn't the
  JSON we expect was the one route still escaping `except LLMError`);
  `backfill_embeddings` is wired — automatic at the top of the `embed` stage when
  `pending_embeddings()` says there is something to do (that stage is what has
  just proved the endpoint is up), plus a manual
  `/api/jobs/defer/backfill_topic_embeddings`; `profile.describe` no longer reads
  "Prefers technical depth" out of an all-negative difficulty distribution (one
  dislike used to do it) and takes an optional slug→label map; blocked keywords
  are LIKE-escaped (`%` in a keyword emptied the feed AND the write queue, `_`
  over-matched); the feed's keyset cursor is taken from the SQL `rank` column
  instead of Python's `math.tanh`, so the comparison never spans two libms.

## Commands

```sh
docker compose up --build -d          # full stack: db (pgvector), migrate (one-shot), web, worker
curl http://127.0.0.1:8200/health     # web app: http://127.0.0.1:8200

# ONLY `web` bind-mounts ./src (with uvicorn --reload). The WORKER runs the code
# baked into the image, so `docker compose restart worker` re-runs the OLD code —
# any change under src/episteme/{worker,recommend,llm,ingest,research}/ needs:
docker compose build worker && docker compose up -d worker
# This is silent and expensive to learn the hard way: on 2026-07-30 two ~10-minute
# propose_topics runs were spent "verifying" a fix the worker did not have. Verify
# after rebuilding, not before:
docker compose exec -T worker python -c "import importlib; print(hasattr(importlib.import_module('episteme.llm.gateway'), '_as_llm_error'))"
# (import the MODULE — `from episteme.llm import gateway` gives the singleton
# instance re-exported by llm/__init__, so hasattr on it is a false negative.)

# Trigger jobs immediately (otherwise: ingestion cron */30, pipeline cron 03:00)
curl -X POST http://127.0.0.1:8200/api/jobs/defer/ingest_all      # or run_pipeline
# (equivalent: docker compose exec worker procrastinate --app=episteme.worker.app.app defer episteme.ingest_all '{}')

# Single pipeline stages (data-driven: each picks up whatever rows are unprocessed,
# so write runs without re-triaging, qa without writing). Optional caps/targets:
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/write?limit=2"        # embed|cluster|triage|write|qa
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/write?story_id=284"   # rewrite (archives old post)
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/qa?post_id=8"         # re-review even if scored
curl -X POST "http://127.0.0.1:8200/api/jobs/defer/ingest_source?source_id=4"

# Recommendation (Phase 4). Topic vocabulary is bootstrapped in TWO phases —
# propose writes a reviewable proposal, apply commits it and rewrites every
# story/post topic array. Read /admin/topics between them.
curl -X POST http://127.0.0.1:8200/api/jobs/defer/propose_topics
curl http://127.0.0.1:8200/api/topics/proposal        # or read /admin/topics
curl -X POST http://127.0.0.1:8200/api/jobs/defer/apply_topics
# Entries created while the embed endpoint was down have no vector, so nothing can
# ever fold into them. The `embed` stage heals them automatically; this forces it.
curl -X POST http://127.0.0.1:8200/api/jobs/defer/backfill_topic_embeddings
# Feedback + profile. The profile is DERIVED: every write here replays the whole
# feedback log, so undo is exact and retuning config reinterprets all history.
curl -X POST "http://127.0.0.1:8200/api/feedback?kind=like&post_id=247"
curl -X POST "http://127.0.0.1:8200/api/feedback/nl?text=more+deep-sea+biology"
# Hand-set one topic weight. ABSOLUTE: replaces the accumulated weight, doesn't
# nudge it; 0 forgets the topic. Same control as the sliders on /tune.
curl -X POST "http://127.0.0.1:8200/api/feedback?kind=set_topic&topic=astronomy&weight=4"
# Hard keyword blocks. unblock_keyword is a counter-event, not a deletion, so it
# also lifts a block a natural-language statement imposed (see the Phase 4 notes).
curl -X POST "http://127.0.0.1:8200/api/feedback?kind=block_keyword&keyword=crypto"
curl -X POST "http://127.0.0.1:8200/api/feedback?kind=unblock_keyword&keyword=crypto"
curl -X DELETE http://127.0.0.1:8200/api/feedback/12   # exact undo + replay
curl http://127.0.0.1:8200/api/profile                 # /tune is the HTML view
curl -X POST http://127.0.0.1:8200/api/profile/rebuild # after retuning weights
curl -X POST http://127.0.0.1:8200/api/jobs/defer/score # rescore the whole feed

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

# Schema migrations (Alembic). `up` applies pending ones automatically, but ONLY
# after the UNREVIEWED marker has been deleted by hand — see the review rule below.
uv run python -m episteme.migrations status
uv run python -m episteme.migrations new -m "add posts.foo"   # DRAFT
uv run python -m episteme.migrations upgrade

# Backup: pg_dump -Fc --compress=zstd into BACKUP_DIR (host bind-mount, default
# ./backups), pruned past backup_retention_days. Worker-owned; no scheduled cron but is a one liner to add.
# Restore a dump with pg_restore.
curl -X POST http://127.0.0.1:8200/api/jobs/defer/backup_database

# Dev (uv-managed venv at .venv). `uv sync` creates/updates it from uv.lock;
# `uv run` auto-syncs before running, so it's the one command you need.
uv sync                                     # create/update .venv from the lockfile
uv run pytest -q                            # all tests
uv run pytest tests/test_llm.py -k retry    # single test
uv run ruff check src tests
uv lock                                     # re-resolve after editing dependencies
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
  **`LLMError` is the gateway's whole error contract** — transport failures are
  wrapped into it (`_as_llm_error`, since 2026-07-30), because every caller writes
  its degradation against `except LLMError` and a raw `httpx.ReadTimeout` would be
  the one failure mode that bypasses all of them. *Origin: the router stalled a
  600s timeout swapping the fast model in; the raw timeout blew through
  `_name_clusters`' handler — which exists precisely so a naming failure degrades
  to member names — and failed the whole job.* Timeouts against a single-GPU box
  that loads models on demand are ordinary; design for them. Measured swap cost:
  ~100s typical, up to 600s worst case (see [handoff-llama-control.md](handoff-llama-control.md) §7).
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
- **Recommendation** (`recommend/`, spec §8) has one organizing rule: **the
  `feedback` table is canonical and everything else is derived from it.**
  `interest_profile` is a cache produced by `profile.replay(events, now)` — a
  pure function — and rebuilt in full on every write; there is deliberately no
  incremental "apply this event" path, because that is exactly what would let the
  cache disagree with what the reader did. Undo is therefore exact (delete the
  row, replay) and changing a half-life or signal weight reinterprets the entire
  history rather than leaving a profile trained under the old constants. Each
  event carries its own payload (embedding + topic/source snapshot) so replay
  never reads mutable state: stories keep absorbing items and posts are
  eventually pruned, and "what I reacted to" is not "what that story is now"
  (`feedback.post_id` is ON DELETE SET NULL for the same reason).
  `topics` is the canonical vocabulary the weights are keyed against — resolved
  in code from whatever a model emits, never trusted from the prompt.
  `scorers.py` is the plugin surface (one implementation + a `scorer_weight_<name>`
  config line); scores are stored on the post while **freshness is applied in the
  feed query**, so ranking never rewrites rows as time passes. `blocks.py` holds
  the single definition of what a hard block excludes, shared by the feed and the
  write queue so the two can't drift.
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
  **Stalled-job recovery** (`worker/maintenance.py`, since 2026-07-21): procrastinate
  writes a job's terminal event only when the worker *finishes* it, so a redeploy
  SIGKILL strands the in-flight job in `doing` forever (this is how ingest_source
  job 1165 stuck on the 07-18 redeploy — worker killed mid-fetch, attempts stays 0).
  A plain worker has nothing that sweeps these, so a periodic `recover_stalled_jobs`
  (`stalled_job_recovery_cron`, default every 5 min) requeues them and prunes the
  dead workers behind them. Detection is heartbeat-based via
  `job_manager.get_stalled_jobs` (the live worker beats every 10s, so the 60s
  `stalled_job_heartbeat_seconds` threshold can never catch a running job; NULL
  worker_id — pre-heartbeat orphans — counts as stalled). Requeue is safe because
  ingest is idempotent and pipeline stages re-pick unprocessed rows. Also
  deferrable manually: `POST /api/jobs/defer/recover_stalled_jobs`.
- **Schema management is Alembic** (since 2026-07-20; replaced bootstrap.py's
  hand-written `RENAME_MIGRATIONS`/`ADDITIVE_MIGRATIONS` DDL lists, whose end
  state is collapsed into the baseline revision `794b362d6e01`). Migrations live
  in `src/episteme/migrations/` — inside the package, so the Dockerfile's
  `COPY src ./src` ships them and `script_location` resolves identically in dev
  and container. There is deliberately **no `create_all`** anywhere: it cannot
  alter existing tables, so keeping both would mean two sources of truth that
  silently disagree on any non-empty database.
  `bootstrap.py` (compose `migrate` one-shot) now: wait for db → adopt-or-upgrade
  → procrastinate schema (guarded; `procrastinate schema --apply` is NOT
  idempotent, and its tables are excluded from autogenerate in `env.py` so the
  two never fight) → seed sources. Two behaviors worth knowing:
  - *Adoption*: a database with tables but no `alembic_version` is **stamped**
    with the baseline, not migrated onto it (it already has that schema). Fires
    at most once, on the pre-Alembic database.
  - *Pre-migration backup*: applying anything to a non-empty database runs
    `pg_dump` first and **aborts if the dump fails** — fail-closed, because a
    migration is the one routine operation that can destroy data faster than it
    can be noticed. `backup_enabled=false` opts out.
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
- **Feed** — spec §8 describes a two-tier design (daily selection, "you're caught up"
  divider, aggregation stream below it); **as built it is a single ranked stream** of
  every published post, features and aggregate cards interleaved as equal units and
  ordered by `web/app.py:_rank_expr`. The divider, diversity and serendipity quotas
  are deferred (Phase 4 note in the spec), so don't go looking for them in the code.
  Infinite scroll is htmx `revealed` sentinels swapping in `/partials/*` pages.
  Falls back to raw source items until the pipeline has output.
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

## Migrations: the review rule (user feedback — hard)

**Never delete a migration's `UNREVIEWED = True` line without having read the
whole file in this session.** That deletion is the approval, and it is the only
thing standing between an autogenerated draft and the database. `alembic upgrade`
refuses to run while the marker is present (enforced in `migrations/env.py`, so
bare `alembic` is gated too, not just the project CLI).

Reviewing means checking, at minimum:

1. **Renames.** `--autogenerate` cannot see them: it emits `drop_column` +
   `add_column`, which deletes the column's data. If a drop+add pair on one
   table is really a rename, replace both with
   `op.alter_column("t", "old", new_column_name="new")`. `new` flags this
   pattern as `possible_rename` in comments directly above the marker.
2. **Type changes.** The generated cast runs against existing rows and can
   truncate or fail.
3. **What the diff cannot see at all**: data backfills, values for existing rows
   under a new NOT NULL column, `CREATE EXTENSION` (the baseline needed one
   added by hand), anything in a JSONB payload.

Workflow (all of it is `uv run python -m episteme.migrations <cmd>`):

```sh
uv run python -m episteme.migrations status              # what exists, what is unreviewed
uv run python -m episteme.migrations new -m "add x"      # autogenerate a DRAFT from models.py
uv run python -m episteme.migrations new --empty -m "backfill y"   # data migration, no diff
uv run python -m episteme.migrations check <rev>         # re-run the hazard analysis
# ... read the file, correct it, delete its UNREVIEWED line ...
uv run python -m episteme.migrations upgrade             # or just `docker compose up`
```

Generating a revision requires a reachable database (autogenerate diffs against
it) but applies nothing. `--empty` needs no diff. Tests in
`tests/test_migrations.py` cover the gate; `test_shipped_revisions_are_reviewed`
fails the suite if an unreviewed revision is ever committed.

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
