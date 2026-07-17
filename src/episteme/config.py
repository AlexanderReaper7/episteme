from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://episteme:episteme@localhost:5432/episteme"
    ingest_cron: str = "*/30 * * * *"
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
    llm_model_main: str = "Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M"
    llm_model_fast: str = "empero-ai_Qwythos-9B-Claude-Mythos-5-1M-GGUF_Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M"
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
    enrich_max_searches: int = 4  # web_search calls per story
    enrich_max_fetches: int = 6  # fetch_page calls per story
    enrich_max_steps: int = 12  # total tool-call turns before forcing a draft
    enrich_fetch_char_limit: int = 6000  # per-page text handed to the model
    enrich_wall_clock_seconds: int = 300  # hard cap on one story's research loop
    # Deterministic demote gate: if source text + gathered dossier is thinner than
    # this, the story is aggregated instead of written (a bare caption that even
    # research couldn't expand). Model-driven demotion proved unreliable — the writer
    # picked "skip" on rich material — so this is a code-level content-volume check.
    min_write_chars: int = 1200

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL is plain libpq form (used by procrastinate/psycopg);
        SQLAlchemy needs the asyncpg driver spelled out."""
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


settings = Settings()
