"""One-shot bootstrap: wait for Postgres, create extension + tables, apply the
procrastinate job-queue schema, seed sources. Idempotent; run by the `migrate`
compose service before web/worker start."""

import asyncio
import logging

from sqlalchemy import text

from .db import SessionLocal, engine, init_db
from .seeds import seed_sources

log = logging.getLogger("episteme.bootstrap")

MAX_ATTEMPTS = 30
RETRY_DELAY_SECONDS = 2.0


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await init_db()
            break
        except Exception as exc:  # DB not up yet — retry
            if attempt == MAX_ATTEMPTS:
                raise
            log.info("Database not ready (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
            await asyncio.sleep(RETRY_DELAY_SECONDS)

    await apply_additive_migrations()
    await apply_job_queue_schema()

    async with SessionLocal() as session:
        added = await seed_sources(session)
    if added:
        log.info("Seeded %d sources", added)
    log.info("Bootstrap complete")


# Poor-man's migrations: create_all only creates missing TABLES, so columns added
# to existing tables are listed here as idempotent DDL. Replace with Alembic when
# schema churn picks up (Phase 2).
ADDITIVE_MIGRATIONS = [
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS http_etag TEXT",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS http_last_modified TEXT",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ",
    # Phase 2 (stories table itself comes from create_all, which runs first)
    "ALTER TABLE source_items ADD COLUMN IF NOT EXISTS story_id INT REFERENCES stories(id)",
]


async def apply_additive_migrations() -> None:
    async with engine.begin() as conn:
        for ddl in ADDITIVE_MIGRATIONS:
            await conn.execute(text(ddl))


async def apply_job_queue_schema() -> None:
    """procrastinate's `schema --apply` is not idempotent — guard on a marker table."""
    async with engine.connect() as conn:
        exists = (
            await conn.execute(text("SELECT to_regclass('procrastinate_jobs')"))
        ).scalar_one()
    if exists:
        return
    from .worker.app import app as job_app

    async with job_app.open_async():
        await job_app.schema_manager.apply_schema_async()
    log.info("Applied procrastinate schema")


if __name__ == "__main__":
    asyncio.run(main())
