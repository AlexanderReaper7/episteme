from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://episteme:episteme@localhost:5432/episteme"
    ingest_cron: str = "*/30 * * * *"
    feed_page_size: int = 20
    http_timeout_seconds: float = 20.0
    http_user_agent: str = "Episteme/0.1 (personal news aggregator)"
    # Global politeness throttle: gap between ANY two outbound source requests,
    # sampled from a normal distribution and clamped to the minimum.
    polite_delay_min_seconds: float = 2.0
    polite_delay_mean_seconds: float = 3.0
    polite_delay_stddev_seconds: float = 1.0
    # Fallback cooldown after a 429 without a Retry-After header.
    rate_limit_cooldown_seconds: int = 3600

    # --- LLM gateway (any OpenAI-compatible server; llama-server in router mode) ---
    llm_base_url: str = "http://host.docker.internal:5001/v1"
    llm_model_writer: str = "Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M"
    llm_model_fast: str = "empero-ai_Qwythos-9B-Claude-Mythos-5-1M-GGUF_Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M"
    llm_model_embed: str = ""  # set when a GGUF embedding model is available
    llm_timeout_seconds: float = 600.0
    llm_max_json_retries: int = 2
    llm_disable_thinking: bool = True

    # --- Pipeline ---
    pipeline_cron: str = "0 3 * * *"  # nightly; idle-aware gating comes in Phase 5
    embed_batch_size: int = 16
    cluster_similarity_threshold: float = 0.82  # cosine similarity to join a story
    cluster_window_days: int = 5
    max_sources_per_story: int = 12
    max_writes_per_run: int = 10
    summarize_above_chars: int = 2500

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL is plain libpq form (used by procrastinate/psycopg);
        SQLAlchemy needs the asyncpg driver spelled out."""
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


settings = Settings()
