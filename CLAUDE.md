# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Episteme: a self-hosted, single-user, LLM-powered personalized science newsfeed. Sources are
ingested continuously; a local LLM (llama-server on the host, port 5001) processes them
overnight into generated articles plus a Google-News-style aggregation stream.

**Read [episteme-architecture.md](episteme-architecture.md) first** — it is the authoritative
spec (data model, pipeline stages, feed-composition rules, roadmap phases, decided constraints).

`docs/` holds investigations whose *conclusions* belong in this file but whose method and
raw data would drown it — e.g.
[docs/llama-cpp-host-vs-docker.md](docs/llama-cpp-host-vs-docker.md) (2026-08-01: should
inference move into Docker? measured — generation is ~13% slower under WSL2, a model-dir
bind mount is disqualifying, `embed` is the one role worth moving; decision still open).

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

- **Host control agent + resource governor built AND live-verified (2026-08-01)** —
  the crossing of the Docker/host boundary, and the first automatic scheduling.
  `hostagent/llama_agent.py` is a single-file FastAPI service run on the HOST
  (`uv run hostagent/llama_agent.py`, loopback :5003, PEP-723 header so uv
  resolves its two deps). It merges two things the docs specified separately —
  the lifecycle controller of [handoff-llama-control.md](handoff-llama-control.md)
  §4b and the "idle monitor" of spec §7 — because they want the same privileges
  on the same box. It is a **sensor and actuator, never a decision-maker**:
  `/resources` reports measurements, `worker/governor.py` owns the policy, so
  every threshold lives in Episteme's config and an agent that dies leaves *no
  opinion* rather than a stale flag. `llm/host.py` is the client, deliberately
  separate from `gateway.py` (lifecycle must not become reachable from inside a
  completion call). **`LLM_HOST_AGENT_URL` empty disables all of it** and
  Episteme behaves exactly as before — verified.
  - *Signals, and where each is valid.* Per-process GPU utilization comes from
    the `\GPU Engine(*)\Utilization Percentage` perf counter, so
    `foreign_gpu_percent` (total minus our own llama-server PIDs) is truthful
    **even while we generate** — which is what makes it a pause signal and not
    only a start gate. VRAM cannot be attributed: `nvidia-smi
    --query-compute-apps` returns `[N/A]` per process under WDDM, and the `GPU
    Process Memory` counter over-reports badly (measured: dwm claiming 22 GB on
    a 10 GB card — it counts committed, not resident). So free VRAM is consulted
    **only while our own models are unloaded**, where the whole figure is by
    definition someone else's. Windows' Game Bar registry
    (`HKCU:\System\GameConfigStore\Children`, 310 entries) intersected with
    running processes is a self-maintaining game watchlist — reported as context
    for the panel, never decided on.
  - *The rule is contention, not presence* (user decision): other work takes
    priority, but only where Episteme would noticeably degrade it. Idle-time
    detection was considered and dropped — someone typing an email is not a
    reason to stop writing articles.
  - *Pause now records `reason` + `since`* (`worker/control.py`). Two very
    different actors pause the pipeline, and without an author the governor
    would lift a pause a human set. `RESOURCE` is the only reason it may clear;
    a legacy `{"paused": true}` row reads as `MANUAL`, which is the safe
    direction. Asymmetric timing on purpose: yield immediately, resume only
    after `resource_resume_quiet_seconds` — restarting a 20 GB model load during
    a lull between two loading screens is worse than waiting.
  - *"Stop" is composed, not new*: `POST /api/llm/backend/stop` sets the pause,
    waits for the worker to finish its current unit, and only then kills the
    processes. On timeout it returns `stopped: false` and leaves everything
    running; the panel then offers an explicit force. Nothing is killed
    mid-generation without a second deliberate click.
  - *Cost budget (measured, and it shaped the design).* `Get-Counter` over all
    621 GPU-engine instances is 3.3s and ~1s of that is PDH's own sampling
    floor, so `/resources` is ~3.5s. It is therefore never on a synchronous
    path: the admin panel loads it as its own htmx fragment and the governor
    polls it on a cron. `Get-NetTCPConnection` was dropped entirely — **3.1s per
    fresh process** (it re-imports NetTCPIP every time) against 259ms for bare
    PowerShell; liveness is a Python socket connect instead, which is ~1ms and a
    truer test. `/status` fell 5.15s → 1.09s and costs zero subprocesses when
    the backend is down, which is exactly when someone is looking at it.
  - **Three defects found live, each fixed with a regression test:**
    1. *Two rapid starts produced FOUR llama-server processes.* A bare "is it
       listening?" check is a TOCTOU race: both requests saw nothing bound and
       both ran the launcher. The launcher's own `Get-NetTCPConnection` guard has
       the identical hole. Every lifecycle route now takes a reentrant lock held
       **through the port wait**, not just the check — releasing after spawning
       would let the next caller observe the not-yet-bound port and launch again.
       (The test fails with 4 launches if the lock is removed — verified.)
    2. *The log pane replaced the entire dashboard.* `.admin-main` carries
       `hx-target="#admin-main"` and **htmx inherits `hx-target`**, so a
       self-replacing fragment relying on the default target swallows the page —
       then polls against an element it deleted (`htmx:targetError` every 3s).
       Every partial here now names `hx-target="this"`. Worth remembering for any
       new admin fragment.
    3. *ANSI escapes in the log file.* llama-server colors its output and
       `--log-file` gets the codes verbatim. Stripped in the agent rather than via
       `--log-colors off`, so the pane is correct however the server was started
       and a human running the launcher in a console keeps their colors.
  - *Launcher changes* (outside this repo, backup at `launch-llama-v2.ps1.bak`):
    `--log-file` + `--log-timestamps` on both servers, logs truncated on start
    (user's choice — llama-server does not rotate), and a `-Detached` switch that
    starts hidden with no console, since a service-started process has none to
    attach to. `-Detached` wins over `-Foreground` (which defaults to `$true`).
  - *Live run (2026-08-01, while Battlefield 6 was running)*: panel read
    `foreign 90.7% / ours 0% / 863 MB free / games: bf6` and correctly showed both
    servers down; start from `/admin` brought both up with PIDs and uptime and the
    log pane filled with real router output; the governor paused for real
    (`reason: resource`, `foreign GPU load 90% >= 25% (bf6)`); forcing the
    thresholds to read "quiet" against the *live* sensor confirmed all three
    resume cases (governor+old → resume, governor+recent → wait out the window,
    **manual → never touched**); graceful stop brought both down and left the
    pause set; three concurrent starts → exactly one launch and exactly 2
    processes.
  - **Not yet verified live**: a pipeline stage actually running after an
    agent-driven start (the GPU was occupied by a game throughout, and loading a
    20 GB model on top of it is precisely what this feature exists to prevent),
    and the graceful stop's *timeout* branch (nothing was mid-generation to make
    it wait).

  **Code-review pass (2026-08-01, fixed + regression-tested, NOT yet
  live-verified)** — nine findings, all against paths the live run never
  exercised because the GPU was busy throughout. The four that would have
  destroyed work or lied to the operator:
  1. *The governor unloaded models mid-generation.* Pause is gentle precisely so
     the current story survives; `unload_models()` was then called
     unconditionally, ripping the model out of VRAM under it. It now takes the
     same guard `/api/pipeline/pause` has — and that guard is now ONE predicate,
     `control.pipeline_job_running`, shared by both (the API keeps a session
     wrapper). When something is running, the worker unloads at its own next unit
     boundary, as it already did.
  2. *The resume window timed the pause, not the quiet.* `since` was stamped once
     and never refreshed, so after 300s of a two-hour game the window had long
     expired and the first momentary dip — a loading screen, an alt-tab —
     resumed straight into it. The pause value now carries `contended_at`
     alongside `since`: `decide` returns a third action, `hold`, for "contended
     while already paused", and the caller re-stamps it (`control.mark_contended`,
     which never moves `since` — that is what the panel shows). Rows written
     before the field read `contended_at` as `since`.
  3. *Two definitions of "holds VRAM" disagreed.* The governor tested
     `== "loaded"` while `unload_models` tested `not in (None, "unloaded")`, so
     during the ~100s a model takes to load the governor attributed our own fresh
     allocation to someone else and paused + unloaded the model it was loading.
     `gateway.holds_vram(row)` is now the single predicate, and `loading` counts
     as loaded — a model halfway into VRAM occupies it just as much.
  4. *The graceful stop paused before finding out it could not stop anything.*
     An absent or unreachable agent — the normal state, it is optional — 503'd
     *after* leaving the pipeline paused as MANUAL, which the governor is
     forbidden to lift, with no llama.cpp problem left to explain it. The agent is
     now pre-flighted before any write, and a failed kill rolls the flag back via
     `control.restore_pause` (a verbatim snapshot restore, so a governor pause is
     not silently re-authored as a manual one). The *timeout* path still keeps the
     pause deliberately.

  Then: `/start` verifies the ports actually bound instead of trusting the exit
  code (PowerShell's default `$ErrorActionPreference` is Continue, so a launcher
  that failed on a missing model exits 0 — it reported `started: true` and threw
  away the captured output that explained it); the host-agent client has **three**
  timeouts instead of one, because reads sit on the dashboard's critical path
  (a hung agent blocked the page for the length of a model load) while `/restart`
  is a stop and a start in one request and could exceed the single ceiling, i.e.
  render a success as a failure; the `*/2` periodic is registered only when the
  governor is actually enabled (720 no-op job rows a day into an unpruned
  `procrastinate_jobs`, drowning the 20-row admin queue view); and `_backend.html`
  now shows the pause the stop button caused — the pause controls live on
  `/admin/jobs`, a different page, so the operator previously got no indication at
  all on the one they were standing on.

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

# llama.cpp lifecycle + logs + GPU sensing, via the HOST control agent.
# The agent runs ON THE HOST (not in compose — it is what starts Episteme's
# dependency, so it cannot share its lifecycle). Installed as a scheduled task
# that starts at logon; `uv run` resolves its PEP-723 deps, so there is no venv
# to maintain. Its own stdout goes to C:\selfhosting\llama-cpp\logs\agent.log.
#   pwsh hostagent/install-task.ps1            # register + start (idempotent, -Force)
#   pwsh hostagent/install-task.ps1 -Remove    # unregister
#   uv run hostagent/llama_agent.py            # or just run it in a console
# Then set LLM_HOST_AGENT_URL=http://host.docker.internal:5003 in .env.
# Everything below 503s cleanly when the agent is absent; the feature is optional.
curl http://127.0.0.1:8200/api/llm/backend                 # ports, PIDs, uptime
curl -X POST http://127.0.0.1:8200/api/llm/backend/start   # idempotent (locked, not just checked)
curl -X POST http://127.0.0.1:8200/api/llm/backend/restart
# Graceful: pauses, waits for the current work unit, THEN kills. Returns
# stopped:false if the unit outlasts llm_graceful_stop_seconds — nothing dies
# mid-generation without the explicit force.
curl -X POST http://127.0.0.1:8200/api/llm/backend/stop
curl -X POST "http://127.0.0.1:8200/api/llm/backend/stop?force=true"
curl "http://127.0.0.1:8200/api/llm/logs?which=router&tail=200"   # or which=embed
curl http://127.0.0.1:8200/api/llm/resources               # ~3.5s: per-process GPU, VRAM, games
# Resource governor (RESOURCE_GOVERNOR_ENABLED=true). Runs on its own cron;
# defer it to exercise the policy now. It pauses with reason=resource and will
# never lift a pause a human set.
docker compose exec worker python -c "import asyncio; from episteme.worker.governor import govern_resources; print(asyncio.run(govern_resources())['reason'])"

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
# status/new/check apply nothing, so they run from the host:
uv run python -m episteme.migrations status
uv run python -m episteme.migrations new -m "add posts.foo"   # DRAFT
# `upgrade` is a CONTAINER command. It dumps the database first, and pg_dump plus
# the bind-mounted /backups live in the image, not on the host — from the host it
# refuses with this instruction rather than migrating unprotected.
docker compose run --rm migrate                          # bootstrap: adopt-or-upgrade + seed
docker compose run --rm migrate python -m episteme.migrations upgrade   # migrate only

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
  (`main`/`fast`/`embed`); config maps roles to model names (`LLM_MODEL_*` env)
  **and to endpoints** (`LLM_<ROLE>_BASE_URL`, falling back to `LLM_BASE_URL`).
  Endpoint topology is config, not code (since 2026-08-01): `endpoint_for(role)`
  resolves the URL, `endpoints()` groups roles by distinct URL, and one httpx
  client is cached per URL — so roles sharing a server share a connection pool,
  and moving any role to its own llama-server is an env change. Nothing
  special-cases `embed`; that it sits alone on :5002 today is just what the
  defaults say. Health (`unavailable_endpoints`, which names the down URLs so the
  gate and the log line share one probe), the admin role table and
  `unload_models` all derive from the resolved URLs. **`unload_models` visits
  every endpoint and lets the server decide**: only llama-server in *router* mode
  reports a per-model `status.value`, so a plain single-model server reports
  nothing loaded and is never sent an unload — no hardcoded notion of which port
  holds VRAM.
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
  - *Pre-migration backup* (`migrations/prebackup.py`, fired from `env.py`):
    applying anything to a non-empty database runs `pg_dump` first and **aborts
    if the dump fails** — fail-closed, because a migration is the one routine
    operation that can destroy data faster than it can be noticed.
    `backup_enabled=false` opts out.
    **It lived in `bootstrap.py` until 2026-08-02 and therefore covered one of
    the three ways a revision gets applied.** The CLI (`cmd_upgrade` called
    `command.upgrade` straight through) and a bare `alembic upgrade head` both
    migrated real data with no dump — and both are documented workflows, which
    is how the `post_audio` migration got applied unprotected. Two things made
    it worse than a missing call: the host has **no pg_dump at all**, and
    `backup_dir` defaults to `/backups`, a container path that resolves to
    `C:\backups` on Windows — so the host CLI could never have produced a
    correct dump even if it had tried. Now it sits in `env.py` beside the review
    gate, for the reason env.py's own docstring gives for the gate: that module
    is the only code every path into alembic runs through. Both protections
    share one trigger, `env.applies_ddl()` (renamed from `_guard_should_run`;
    the `skip_review_guard` config attribute is now `applies_no_ddl`, set by
    `stamp` and by `new`, which connect but apply no DDL). Skips the dump only
    when it can prove there is nothing to protect — no `sources` table (empty
    database) or current revision == the resolved target (so `docker compose up`
    at head costs nothing). An **unresolvable** target (a relative `+1`, a
    downgrade) reads as "assume something changes" and takes the dump.
    *Verified live 2026-08-02*: host-side pending upgrade blocked with the
    container command (read the real revision `bed89c48b376` off the live DB);
    host-side no-op upgrade at head still passed without dumping; the same call
    in the container wrote a valid 15.9 MB dump (162 objects per `pg_restore
    --list`) and pruned nothing.
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

Workflow. Everything that only *reads or writes files* runs from the host;
**`upgrade` runs in the container**, because it takes a pg_dump first and
pg_dump plus the `/backups` mount exist in the image, not on the host:

```sh
uv run python -m episteme.migrations status              # what exists, what is unreviewed
uv run python -m episteme.migrations new -m "add x"      # autogenerate a DRAFT from models.py
uv run python -m episteme.migrations new --empty -m "backfill y"   # data migration, no diff
uv run python -m episteme.migrations check <rev>         # re-run the hazard analysis
# ... read the file, correct it, delete its UNREVIEWED line ...
docker compose run --rm migrate                          # or just `docker compose up`
```

Running `upgrade` from the host is not merely discouraged — it stops with the
container command, because it cannot take the backup. That refusal is the whole
point: the alternative, and what actually happened before 2026-08-02, is
migrating real data unprotected because the safety net was in a file that
particular entry point didn't touch.

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
