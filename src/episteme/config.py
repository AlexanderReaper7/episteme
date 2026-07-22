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
    feed_page_size: int = 20
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
    llm_base_url: str = "http://host.docker.internal:5001/v1"
    # Embeddings use a dedicated always-resident llama-server (CPU-only model,
    # spawned by launch-llama-v2.ps1 on :5002). The router's --models-max counts
    # models globally with no per-model exemption, so capping it at 1 — required
    # so the fast and main models never share VRAM — would otherwise evict the
    # embedder. Point this at llm_base_url to serve embeds from the router again.
    llm_embed_base_url: str = "http://host.docker.internal:5002/v1"
    llm_model_main: str = "Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M"
    llm_model_fast: str = "Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M"
    # 4B (2560 dims, gateway truncates to EMBEDDING_DIM) preferred for quality;
    # swap to "Octen-Embedding-0.6B.f16" (native 1024) if speed matters more.
    llm_model_embed: str = "Octen-Embedding-4B.Q8_0"
    llm_timeout_seconds: float = 600.0
    llm_max_json_retries: int = 2
    llm_disable_thinking: bool = True
    # Observability: persist every gateway call (prompts, responses, tokens, timing)
    # to the llm_calls table for the admin provenance view. Embeds log batch sizes
    # only. Rows older than the retention window are pruned at the end of each
    # pipeline run.
    llm_log_enabled: bool = True
    llm_log_retention_days: int = 30

    # --- Pipeline ---
    pipeline_cron: str = "0 3 * * *"  # nightly; idle-aware gating comes in Phase 5
    embed_batch_size: int = 16
    cluster_similarity_threshold: float = 0.82  # cosine similarity to join a story
    cluster_window_days: int = 5
    max_sources_per_story: int = 12
    summarize_above_chars: int = 2500
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
    qa_max_rounds: int = 2  # review->revise->re-review cycles per post
    qa_viewport_width: int = 1100
    # Where the worker reaches the web app to render posts (compose service DNS).
    web_internal_url: str = "http://web:8200"

    # --- TTS narration (Fish Audio; the `narrate` pipeline stage) ---
    # Reads a published feature post, renders its spoken script (deterministic for
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
