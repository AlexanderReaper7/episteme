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
    # EMBEDDING_DIM bump 768 -> 1024 (2026-07, Octen embedders). Old-dim vectors
    # cannot be cast, so they are nulled; the pipeline re-embeds NULLs anyway.
    # Guarded on the current column type to stay idempotent (this list runs on
    # every `up`, and an unconditional USING NULL would wipe embeddings).
    """
    DO $$
    BEGIN
        IF (SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
            WHERE a.attrelid = 'source_items'::regclass
              AND a.attname = 'embedding') <> 'vector(1024)' THEN
            ALTER TABLE source_items ALTER COLUMN embedding TYPE vector(1024) USING NULL;
        END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
        IF (SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
            WHERE a.attrelid = 'stories'::regclass
              AND a.attname = 'centroid') <> 'vector(1024)' THEN
            ALTER TABLE stories ALTER COLUMN centroid TYPE vector(1024) USING NULL;
        END IF;
    END $$;
    """,
    # Phase 3 research-writer: story ranking + gathered-research provenance.
    "ALTER TABLE stories ADD COLUMN IF NOT EXISTS rank_score DOUBLE PRECISION",
    "ALTER TABLE stories ADD COLUMN IF NOT EXISTS research_notes JSONB",
    # Observability: tool-loop calls store message deltas chained by chain_id/seq.
    "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS chain_id VARCHAR(36)",
    "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS seq INT",
    # Phys.org needs TLS impersonation (seeds.py explains why); the seed only runs
    # on an empty table, so flip the existing row, clear the superseded disguise_ua
    # key, and drop the cooldown its 429s left.
    """
    UPDATE sources
       SET config = (config - 'disguise_ua') || '{"http_mode": "impersonate"}'::jsonb,
           cooldown_until = NULL
     WHERE name = 'Phys.org'
       AND config->>'http_mode' IS DISTINCT FROM 'impersonate'
    """,
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
