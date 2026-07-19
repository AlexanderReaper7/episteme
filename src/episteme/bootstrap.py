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
    await wait_for_db()
    # Renames must run BEFORE create_all: create_all would otherwise create an
    # empty table under the new name and strand the data under the old one.
    await apply_rename_migrations()
    await init_db()
    await apply_additive_migrations()
    await apply_job_queue_schema()

    async with SessionLocal() as session:
        added = await seed_sources(session)
    if added:
        log.info("Seeded %d sources", added)
    log.info("Bootstrap complete")


async def wait_for_db() -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # DB not up yet — retry
            if attempt == MAX_ATTEMPTS:
                raise
            log.info("Database not ready (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
            await asyncio.sleep(RETRY_DELAY_SECONDS)


# Idempotent renames, applied before create_all (see main()).
RENAME_MIGRATIONS = [
    # Phase 2.5: articles -> posts (+ `kind`, added in ADDITIVE_MIGRATIONS).
    """
    DO $$
    BEGIN
        IF to_regclass('articles') IS NOT NULL AND to_regclass('posts') IS NULL THEN
            ALTER TABLE articles RENAME TO posts;
        END IF;
    END $$;
    """,
]


async def apply_rename_migrations() -> None:
    async with engine.begin() as conn:
        for ddl in RENAME_MIGRATIONS:
            await conn.execute(text(ddl))


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
    # Phase 2.5: post kinds (feature = long-form article; more kinds in Phase 4).
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'feature'",
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
    # Post-scoped provenance: calls carry the post they produced; archived posts
    # record when they were superseded so retention can age them out.
    "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS post_id INT",
    "CREATE INDEX IF NOT EXISTS ix_llm_calls_post_id ON llm_calls (post_id)",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
    # Write-side calls are grouped per generation attempt so the post stamp is
    # exact (a failed attempt's calls stay off later posts' provenance pages).
    "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS attempt_id VARCHAR(36)",
    # Retention pin (user override): pinned posts/calls survive the prune.
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE",
    # Backfill pre-attempt_id rows by timestamp (attempt-tagged rows are excluded:
    # if they are still unstamped, their attempt produced no post — sweeping them
    # into the next post by timestamp is exactly the misattribution the attempt id
    # exists to prevent). Write-side calls precede their post -> earliest post
    # generated after the call; qa reviews an existing post -> latest post
    # generated before the call. 'research' is the pre-2.5 tool-loop stage name,
    # retired but present in old rows.
    """
    UPDATE llm_calls c SET post_id = (
        SELECT p.id FROM posts p
        WHERE p.story_id = c.story_id AND p.generated_at >= c.created_at
        ORDER BY p.generated_at LIMIT 1)
     WHERE c.post_id IS NULL AND c.story_id IS NOT NULL AND c.attempt_id IS NULL
       AND c.stage IN ('condense', 'research', 'write')
    """,
    """
    UPDATE llm_calls c SET post_id = (
        SELECT p.id FROM posts p
        WHERE p.story_id = c.story_id AND p.generated_at <= c.created_at
        ORDER BY p.generated_at DESC LIMIT 1)
     WHERE c.post_id IS NULL AND c.story_id IS NOT NULL AND c.stage = 'qa'
    """,
    "UPDATE posts SET archived_at = now() WHERE status = 'archived' AND archived_at IS NULL",
    # All content is a post (2026-07-18): aggregate cluster cards become
    # identity-only post rows (kind='aggregate', no stored content — the card
    # renders from the story's items). Content columns go nullable for them,
    # and existing aggregated stories get their card minted here.
    "ALTER TABLE posts ALTER COLUMN title DROP NOT NULL",
    "ALTER TABLE posts ALTER COLUMN summary DROP NOT NULL",
    "ALTER TABLE posts ALTER COLUMN difficulty DROP NOT NULL",
    """
    INSERT INTO posts (story_id, kind, status, topics, sections, reading_time_minutes)
    SELECT s.id, 'aggregate', 'published', '[]'::jsonb, '[]'::jsonb, 1
      FROM stories s
     WHERE s.status = 'aggregated'
       AND NOT EXISTS (SELECT 1 FROM posts p
                       WHERE p.story_id = s.id AND p.status = 'published')
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
