from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://episteme:episteme@localhost:5432/episteme"
    ingest_cron: str = "*/30 * * * *"
    # Stalled-job recovery: a redeploy SIGKILLs the worker, and procrastinate only
    # writes a job's terminal row when the worker *finishes* it — so an in-flight
    # job is stranded in `doing` forever, with nothing in a plain worker to sweep
    # it (this is how ingest_source job 1165 stuck on the 2026-07-18 redeploy). A
    # periodic sweep requeues such jobs. Detection is heartbeat-based: the live
    # worker beats every 10s, so the 60s threshold can never catch a running job.
    stalled_job_recovery_cron: str = "*/5 * * * *"
    stalled_job_heartbeat_seconds: float = 60.0
    # Job-history retention, tiered by what a row is worth in hindsight rather
    # than by age alone. procrastinate never prunes finished jobs, and the two
    # self-firing housekeeping crons write ~1100 rows/day between them against
    # ~5 real pipeline jobs — so an age-only window either keeps a year of
    # heartbeats or throws away the pipeline history with them. A finished job is
    # deleted when its terminal event is older than its class's window; its
    # `procrastinate_events` rows go with it (ON DELETE CASCADE). Waiting and
    # running jobs are never touched. Any window <= 0 keeps that class forever.
    # Failures outlive successes in every class: they are the rows you go looking
    # for, and 42 of them sat unnoticed here behind a page that only ever showed
    # the most recent 50 jobs.
    job_history_maintenance_days: int = 2  # governor, stalled-recovery, schedulers
    job_history_ingest_days: int = 7  # ingest_all, ingest_source
    job_history_work_days: int = 90  # pipeline stages, topics, backup
    job_history_failed_days: int = 90  # any class, any non-succeeded outcome
    job_history_prune_cron: str = "30 4 * * *"  # after the 03:00 pipeline
    feed_page_size: int = 20
    # Background-revalidation grace window (seconds) for the feed + post HTML pages
    # (web/app.py). These pages are served `max-age=0, stale-while-revalidate=N`:
    # every navigation revalidates against the ETag (no blind freshness window), but
    # within N seconds of a cache entry going stale the browser may render the cached
    # copy INSTANTLY while it revalidates in the background — so the hover-prefetch
    # makes the click paint with no network wait, yet a changed feed/article is always
    # re-checked and self-heals on the next navigation. Set to 0 to force a blocking
    # revalidation on every navigation (correctness identical, just not instant).
    html_cache_swr_seconds: int = 30
    # Timestamps are stored and compared in UTC everywhere; this is a *display-only*
    # override so the admin/provenance pages render local wall-clock instead of UTC
    # (web/templating.py `dt` filter). Any IANA name; empty string keeps raw UTC.
    display_timezone: str = "Europe/Stockholm"
    http_timeout_seconds: float = 20.0
    http_user_agent: str = "Episteme/0.1 (personal news aggregator)"
    # Some publishers put a bot-detector in front of feeds their robots.txt
    # permits, and it fingerprints the TLS handshake — an honest httpx client is
    # rejected no matter its User-Agent (Phys.org 429s every httpx request but
    # serves curl_cffi with a browser TLS fingerprint). `http_mode="impersonate"`
    # on a source (see ingest.http) presents this Chrome profile instead. It's
    # opt-in per source, never the global default. Volume is unchanged — the
    # throttle and cooldowns still apply; only the fingerprint differs.
    impersonate_profile: str = "chrome"  # curl_cffi target; e.g. chrome / chrome131 / safari
    # When a source's fetch is blocked (403/429), escalate its http_mode one tier
    # (polite -> impersonate) and retry once, then persist the mode that worked.
    # Encodes the standing policy: a source that fails politely gets stronger,
    # still-non-destructive means before we give up on it.
    http_escalate_on_block: bool = True
    # Global politeness throttle: gap between ANY two outbound source requests,
    # sampled from a normal distribution and clamped to the minimum.
    polite_delay_min_seconds: float = 2.0
    polite_delay_mean_seconds: float = 3.0
    polite_delay_stddev_seconds: float = 1.0
    # Fallback cooldown after a 429 without a Retry-After header.
    rate_limit_cooldown_seconds: int = 3600

    # --- LLM gateway (any OpenAI-compatible server; llama-server in router mode) ---
    # Default endpoint: every role is served from here unless it has its own URL
    # below. Which roles share a server is pure topology, so it lives in config —
    # nothing in the code knows that `embed` is the one that tends to be separate.
    llm_base_url: str = "http://host.docker.internal:5001/v1"
    # Per-role overrides; empty string = use llm_base_url. Set one when a role
    # needs its own server: a different machine, a different llama-server build,
    # a model that must stay resident while the others swap.
    llm_main_base_url: str = ""
    llm_fast_base_url: str = ""
    # Embeddings default to a dedicated always-resident llama-server (CPU-only
    # model, spawned by launch-llama-v2.ps1 on :5002). The router's --models-max
    # counts models globally with no per-model exemption, so capping it at 1 —
    # required so the fast and main models never share VRAM — would otherwise
    # evict the embedder. Set this to "" to serve embeds from llm_base_url again.
    llm_embed_base_url: str = "http://host.docker.internal:5002/v1"
    llm_model_main: str = "Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M"
    llm_model_fast: str = "Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M"
    # 4B (2560 dims, gateway truncates to EMBEDDING_DIM) preferred for quality;
    # swap to "Octen-Embedding-0.6B.f16" (native 1024) if speed matters more.
    llm_model_embed: str = "Octen-Embedding-4B.Q8_0"
    # The user-facing assistant (llm/chat.py). Both empty means "whatever main
    # uses", which is the default and not laziness: sharing main's server is what
    # keeps an interactive turn from forcing a model swap while `write` is running.
    # The cost is the other direction — chatting while the FAST model is resident
    # (triage, condense) does force a swap, ~100s each way, and stalls the stage.
    # Point these at a third llama-server to decouple the assistant entirely.
    llm_chat_base_url: str = ""  # "" -> llm_main_base_url -> llm_base_url
    llm_model_chat: str = ""  # "" -> llm_model_main
    llm_timeout_seconds: float = 600.0
    llm_max_json_retries: int = 2
    llm_disable_thinking: bool = True
    # Observability: persist every gateway call (prompts, responses, tokens, timing)
    # to the llm_calls table for the admin provenance view. Embeds log batch sizes
    # only. Rows older than the retention window are pruned at the end of each
    # pipeline run.
    llm_log_enabled: bool = True
    llm_log_retention_days: int = 30

    # --- Host control agent (hostagent/llama_agent.py; llm/host.py is the client) ---
    # Episteme is in Docker, llama.cpp is on the Windows host: a container cannot
    # start a host process or read its console. This URL is that crossing — a
    # loopback service on the host exposing lifecycle, logs and GPU measurements.
    # EMPTY DISABLES THE WHOLE FEATURE: every route, panel and the governor become
    # no-ops and Episteme behaves exactly as it did before the agent existed. The
    # agent is optional infrastructure, never a dependency.
    llm_host_agent_url: str = ""  # e.g. http://host.docker.internal:5003
    # Three timeouts, because reads and actions want opposite things (see
    # llm/host.py). Reads are on the dashboard's critical path — /status rides
    # the page load, /logs is polled once a second behind the log stream — so a
    # hung agent must give up in seconds rather than take the admin page down
    # with it.
    llm_host_agent_read_timeout_seconds: float = 15.0
    # Actions block until the host has finished: the agent holds /start until
    # both ports answer (launcher 120s + port wait 60s worst case), so a ceiling
    # sized like a read would report a successful start as a failure.
    llm_host_agent_timeout_seconds: float = 240.0
    # /restart is a stop and a start inside one request, so ~the sum of both.
    llm_host_agent_restart_timeout_seconds: float = 360.0
    llm_log_tail_lines: int = 300
    # How often the log stream asks the agent for the bytes written since its
    # last offset. This is the browser-invisible leg: the pane is pushed over
    # SSE, and only this hop polls (a delta read is a file seek, no PowerShell).
    llm_log_stream_interval_seconds: float = 1.0
    # A graceful stop pauses the pipeline and waits for the worker to finish its
    # current unit — one story, measured at up to ~570s for a main-model write —
    # so the wait is bounded and reports back rather than killing anything. The
    # caller (admin panel) then offers an explicit force.
    llm_graceful_stop_seconds: float = 120.0

    # --- Benchmarking (bench/, docs/benchmarks/plan.md) ---
    # No client timeout could distinguish a hung server from a legitimately slow
    # one: one longctx repetition measured 7.5 minutes on the 35B and ~24 on the
    # dense 27B. So the ceiling is generous and CANCELLATION is the real control,
    # which is why every benchmark request streams (dropping the connection is
    # the only abort llama-server offers, and a non-streaming request has nothing
    # to notice the drop on).
    bench_request_timeout_seconds: float = 7200.0
    # How often, mid-stream, we write live progress and re-read the cancel flag.
    # Both cost a database round trip, so this is a time budget rather than a
    # per-chunk check: at 40 tok/s that would be 40 queries a second to answer a
    # question whose answer changes at human speed.
    bench_poll_interval_seconds: float = 0.5
    # Decode curve resolution. Per-token timings on a 16 tok/s model are mostly
    # chunk-boundary jitter, and 2048 points describe nothing 64 do not.
    bench_decode_bucket: int = 32
    bench_predict_tokens: int = 256  # generated per timed sample
    # Rungs of the prefill ladder. Nominal: the x-axis is the `prompt_n` the
    # server measured, since truncation is character-proportional and every model
    # tokenizes differently.
    bench_ladder_rungs: list[int] = [2048, 4096, 8192, 16384, 32768]
    # A benchmark holds the interactive lease so the governor cannot unload the
    # model underneath it. Longer than a chat turn's (a run is minutes to hours)
    # but still a TTL, refreshed by a background task: a worker that dies must
    # not strand the GPU until someone notices.
    bench_lease_seconds: float = 300.0

    # --- Resource governor (worker/governor.py; architecture §7 "Scheduling") ---
    # Yields the GPU to whatever else is using it. The rule the user set: other
    # work takes priority, but only where Episteme would *noticeably* degrade it —
    # so this measures resource contention, NOT whether someone is at the keyboard.
    # Requires llm_host_agent_url; inert without it.
    resource_governor_enabled: bool = False
    resource_governor_cron: str = "*/2 * * * *"
    # Foreign GPU utilization (everything except our own llama-server processes)
    # at which we yield. Per-process attribution makes this valid even while we
    # are generating, which is what lets it work as a pause signal and not just a
    # start gate.
    resource_gpu_busy_percent: float = 25.0
    # Headroom needed to load a decode model. Only consulted when our own models
    # are UNLOADED: VRAM cannot be attributed per process (measured — the Windows
    # counter reported 22GB for dwm on a 10GB card), so while we hold models the
    # number says nothing about contention and is ignored rather than guessed at.
    resource_min_free_vram_mb: int = 6000
    # How long the GPU must stay quiet before a resource pause lifts. Asymmetric
    # on purpose: yield immediately, return slowly, so a lull between two loading
    # screens doesn't restart a 20GB model load on top of a running game.
    resource_resume_quiet_seconds: int = 300

    # --- Pipeline ---
    pipeline_cron: str = "0 3 * * *"  # nightly; idle-aware gating comes in Phase 5
    # Matsedel reads every weekday, and skips a kitchen whose week is already
    # whole, so a normal week still costs one read of each site (0054). It ran
    # Monday 04:00 local until 2026-09-05, which was before any kitchen had
    # published and had no second chance until the following Monday.
    # Crons are evaluated in the WORKER's zone, which is UTC, so 06:00 here is
    # 08:00 in Vanersborg. That is two hours after Monday's own post publishes at
    # 06:00 local, which is the deliberate trade: the alternative is reading
    # before the kitchens are awake and being a day late every time. A kitchen
    # that publishes after 08:00 is picked up by the next morning's read.
    # A plugin's schedule is in core's settings because procrastinate needs it at
    # import time and a `correspondents` row is read at run time; the hours a post
    # publishes and expires at, which nothing needs early, are on the row.
    matsedel_cron: str = "0 6 * * 1-5"
    embed_batch_size: int = 16
    cluster_similarity_threshold: float = 0.82  # cosine similarity to join a story
    cluster_window_days: int = 5
    max_sources_per_story: int = 12
    # Runaway guard on the source text handed to the writer, per item and per
    # story. NOT a condensing threshold: the fast-model condense pass that used to
    # sit here is gone (0050). Measured on the 183 written stories, source text is
    # 4.2k chars at the median and 26k at the maximum, so nothing real reaches
    # these - they exist so one pathological 128k-char feed item cannot eat the
    # 65k-token window.
    max_source_chars_per_item: int = 40000
    max_source_chars_per_story: int = 80000
    # How much of a cluster the aggregate summarizer reads. Its output is 3-5
    # sentences either way, and the `fast` model is doing the reading.
    max_summary_source_chars: int = 24000
    # Writer works the ranked candidate queue (best quality_score first) until this
    # wall-clock budget is spent — the nightly window decides how many get written.
    # max_writes_per_run is now a hard safety cap, not the primary limit.
    write_budget_seconds: int = 3600
    max_writes_per_run: int = 30

    # --- Research agent (writer enrichment) ---
    searxng_url: str = "http://host.docker.internal:8080"
    enrich_enabled: bool = True
    enrich_max_searches: int = 8  # web_search calls per story
    enrich_max_fetches: int = 8  # fetch_page calls per story
    enrich_max_steps: int = 12  # total tool-call turns before forcing a draft
    enrich_fetch_char_limit: int = 6000  # per-page text handed to the model
    enrich_wall_clock_seconds: int = 300  # hard cap on one story's research loop
    # Deterministic demote gate: if source text + gathered dossier is thinner than
    # this, the story is aggregated instead of written (a bare caption that even
    # research couldn't expand). Model-driven demotion proved unreliable — the writer
    # picked "skip" on rich material — so this is a code-level content-volume check.
    min_write_chars: int = 1200

    # --- User-facing assistant (llm/chat.py, web/chat.py) ---
    # Budgets follow the per-stage pattern (enrich_*, qa_*) rather than inventing
    # a new shape. Smaller than the writer's because a human is watching: a turn
    # that thinks for four minutes has already failed as a conversation.
    chat_max_steps: int = 8  # tool-call turns before the model must answer
    chat_wall_clock_seconds: int = 240  # hard cap on one turn's tool loop
    chat_max_searches: int = 4  # web_search calls per turn
    chat_max_fetches: int = 4  # fetch_page calls per turn
    chat_history_turns: int = 20  # messages replayed into a new turn's context
    # How long one interactive turn claims the models for (worker/control.py).
    # A TTL, not a lock: a web process that dies mid-turn must not strand VRAM.
    # Refreshed on every turn, so a long conversation holds continuously.
    chat_lease_seconds: int = 180

    # --- Interest profile (recommend/profile.py) ---
    # The profile is REPLAYED from the feedback log, so these are not "learning
    # rates" that bake into stored state — changing any of them and rebuilding
    # reinterprets the entire history. Tune freely.
    # Half-life of a signal's influence: an interest fades unless reinforced, so
    # a phase of curiosity two years ago cannot hold the feed hostage today.
    feedback_half_life_days: float = 90.0
    # Per-signal strength. Explicit topic steering counts for more than a single
    # like, and a dislike is not a mirror-image of a like: this feed optimizes
    # learning value, so it should be readier to add than to subtract.
    # (There is deliberately no `feedback_save_weight`: a save is a bookmark, not
    # approval, and it feeds nothing into the profile — see profile._CONTENT_SIGNALS.)
    feedback_like_weight: float = 1.0
    feedback_dislike_weight: float = 0.8
    feedback_topic_step: float = 2.0  # more_topic / less_topic
    # A like/dislike also nudges the post's topics and sources, but weakly — it is
    # a signal about one post, not a declaration about a whole subject area.
    feedback_topic_spillover: float = 0.35
    feedback_source_step: float = 0.3
    # Clamp on any single topic/source weight after replay, so a run of feedback
    # on one subject can't crowd everything else out of the feed.
    profile_weight_clamp: float = 6.0

    # --- Scoring + feed ranking (recommend/scorers.py, web/app.py) ---
    # Each registered Scorer's weight is looked up as `scorer_weight_<name>`, so
    # adding a signal is one implementation plus one line here (spec §11). A
    # scorer with no weight is inert rather than an error.
    scorer_weight_liked: float = 1.0
    scorer_weight_disliked: float = 1.0  # magnitude; the scorer returns a negative
    scorer_weight_topic: float = 1.0
    scorer_weight_source: float = 0.5
    scorer_weight_difficulty: float = 0.5
    scorer_weight_quality: float = 0.4
    scorer_weight_authority: float = 0.3
    # How far the profile may move a post in the feed, in hours of apparent
    # recency: a perfectly-matching post ranks as if it were this much newer, a
    # badly-matching one as if it were this much older, and nothing exceeds that
    # (see web.app._rank_expr). At 36h the profile reorders items within a day or
    # so and the stream stays legibly chronological; raise it to let affinity
    # reach across more days, at the cost of a feed whose head stops moving.
    feed_freshness_tau_hours: float = 36.0
    # Raw affinity at which that ceiling is roughly reached (tanh saturation):
    # 2.0 means an affinity of ±4 is already ~96% of the maximum shift. Lower it
    # to make weak preferences bite sooner.
    feed_affinity_scale: float = 2.0
    # The MOST reader affinity may move a story in the write queue, in points of
    # triage's ~0-10 quality score (the term is bounded, see
    # pipeline._rank_write_queue). At 2.0 affinity decides between comparable
    # candidates for the night's main-model budget but can never put a weak story
    # ahead of a strong one on subject alone. 0 disables it entirely.
    write_queue_affinity_weight: float = 2.0
    # Coalescing window for the rescore a feedback click triggers. Reading is
    # bursty — a reader works down the feed liking half a dozen cards — and each
    # signal alone would enqueue a full-corpus pass, so signals arriving within
    # this window ride on the first one's job (see scoring.defer_rescore). The
    # profile is rebuilt on every signal regardless; this only paces the pass that
    # writes scores back onto posts, so the ceiling on staleness is exactly this
    # many seconds. 0 disables coalescing.
    rescore_debounce_seconds: int = 20

    # --- Topics (canonical vocabulary; recommend/topics.py) ---
    # Cosine similarity at which a raw model-emitted topic label is folded into an
    # existing vocabulary entry instead of creating a new one. Higher than the
    # story-clustering threshold on purpose: topic strings are short, so their
    # embeddings sit closer together than document embeddings do, and a loose
    # threshold would collapse genuinely distinct fields into one weight.
    topic_match_threshold: float = 0.88
    # The dedup turn: a tagging model is never shown the vocabulary (that biased it
    # into parroting the list's head — see recommend/topics._review_matches), so
    # consolidation happens here instead. A label that misses the fold threshold but
    # lands within this band of some existing entry is put to the fast model, which
    # may only pick from the offered candidates. Below the band nothing is offered
    # and the label becomes a new entry, exactly as before this tier existed.
    topic_review_enabled: bool = True
    topic_review_threshold: float = 0.62
    topic_review_candidates: int = 5
    # How many vocabulary entries the natural-language feedback parser is shown
    # (most-used first) so a reader's own wording maps onto topics they already
    # have. Deliberately NOT shown to triage or the writer: there the list is a
    # prior on what the tags should be, and the model follows it instead of the
    # story. The prompt cost is per call, so it is capped.
    topic_vocabulary_prompt_limit: int = 80
    # Bootstrap clustering: similarity at which two existing free-text topic labels
    # are considered the same concept when building the initial vocabulary.
    topic_bootstrap_threshold: float = 0.86

    # --- Backups (the worker owns backups; it already runs procrastinate, so no
    # separate service). Custom format (-Fc) is restore-selective via pg_restore
    # and, on PG18/PGDG pg_dump, compresses with zstd instead of the default gzip
    # (--compress=zstd:<level>) — the WAL is already zstd-compressed
    # (wal_compression=zstd in compose), this brings the base dumps in line. Dumps
    # land in backup_dir (bind-mounted to the host in compose) and are pruned past
    # the retention window. Manual-only in alpha: POST /api/jobs/defer/backup_database
    # (no periodic cron registered yet — see worker/backup.py).
    backup_enabled: bool = True
    backup_cron: str = "0 5 * * *"  # unused until a scheduled task is wired up
    backup_dir: str = "/backups"
    backup_retention_days: int = 14  # <=0 keeps every dump
    backup_zstd_level: int = 19  # pg_dump --compress=zstd:<level> (1..22)

    # --- QA stage (main model reviews the rendered post; spec §7 stage 5) ---
    # Requires vision on the main model (--mmproj in models-preset.ini) and headless
    # Chromium in the worker image. Failures are per-post and non-fatal.
    qa_enabled: bool = True
    qa_viewport_width: int = 1100
    # Budgets on the review's tool loop (worker/qa.py). Editing is section-addressed,
    # so these bound the work, not the shape of a fix: a post needing eight small
    # corrections costs eight edits, where the old whole-body revision cost one round
    # and re-emitted everything (including the quiz it could not see).
    qa_max_steps: int = 10  # tool-call turns before the verdict is forced
    qa_max_edits: int = 12  # section mutations per post
    qa_max_screenshots: int = 3  # renders per post, INCLUDING the opening one
    qa_wall_clock_seconds: int = 300  # hard cap on one post's review
    # Where the worker reaches the web app to render posts (compose service DNS).
    web_internal_url: str = "http://web:8200"

    # --- TTS narration (Fish Audio; the `narrate` pipeline stage) ---
    # Reads a published article post, renders its spoken script (deterministic for
    # now — a future LLM "preprocessing" pass will emit an emotion/pronunciation-
    # marked script instead), synthesizes it via Fish Audio, and stores the MP3
    # under audio_dir (bind-mounted to the host in compose). Manual-only until
    # tts_enabled flips on: defer with POST /api/jobs/defer/narrate?post_id=N.
    # Uses the external Fish API, not the local llama-server, so it is exempt from
    # the LLM-availability gate and imposes no VRAM/model-residency cost.
    tts_enabled: bool = False  # when true, narrate runs in the nightly orchestrator
    fish_api_key: str = ""
    # Sent verbatim as the Fish `model` header. The free tier is a distinct model
    # key: "s2.1-pro-free" synthesizes on free credits, while "s2.1-pro" (and the
    # older s2-pro / speech-1.6) return 402 without a paid balance. Passed as a
    # free string — the pinned SDK's typed backends don't list either of these.
    tts_model: str = "s2.1-pro-free"
    # Voices are a client-selectable catalog stored in the DB (the `voices` table,
    # seeded by seeds.seed_voices; accessed via tts.voices). This optionally
    # overrides which catalog voice the nightly stage / selector defaults to;
    # empty = the lowest sort_order enabled voice (David Attenborough). Per-post
    # on-demand narration passes its own voice, so this only sets the default.
    tts_default_voice: str = ""
    # Audio is streamed live from Fish's WebSocket endpoint (/v1/tts/live) and
    # tee'd to the on-disk cache. opus @ 64 kbps keeps the stream light; the
    # pinned SDK can't request opus, so tts.fish frames the protocol directly.
    tts_fish_base_url: str = "https://api.fish.audio"
    tts_audio_format: str = "opus"  # opus | mp3 | wav | pcm
    tts_opus_bitrate: int = 64000  # bps: 24000 | 32000 | 48000 | 64000 | -1000 (auto)
    tts_mp3_bitrate: int = 128  # 64 | 128 | 192 (only when tts_audio_format=mp3)
    tts_latency: str = "balanced"  # normal (best quality) | balanced | low
    audio_dir: str = "/media"  # narration audio lands here (host bind-mount in compose)

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL is plain libpq form (used by procrastinate/psycopg);
        SQLAlchemy needs the asyncpg driver spelled out."""
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


settings = Settings()
