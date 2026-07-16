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

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL is plain libpq form (used by procrastinate/psycopg);
        SQLAlchemy needs the asyncpg driver spelled out."""
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


settings = Settings()
