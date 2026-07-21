"""One-shot bootstrap: wait for Postgres, bring the schema to head, apply the
procrastinate job-queue schema, seed sources. Idempotent; run by the `migrate`
compose service before web/worker start.

Schema changes are Alembic's job (see episteme/migrations/). This module decides
*when* to run them and what has to be true first:

* a database that predates Alembic is **adopted**, not rebuilt -- it already has
  the baseline schema, so it gets stamped rather than migrated onto it;
* anything with real data in it is **backed up before** migrations run, because
  a migration is the one routine operation that can destroy data faster than it
  can be noticed.

Alembic is invoked through `asyncio.to_thread`: env.py drives an async engine via
`asyncio.run`, which cannot be nested inside the loop this module already runs on.
"""

import asyncio
import logging

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text

from .config import settings
from .db import SessionLocal, engine
from .migrations import alembic_config
from .seeds import seed_sources, seed_voices

log = logging.getLogger("episteme.bootstrap")

MAX_ATTEMPTS = 30
RETRY_DELAY_SECONDS = 2.0

# Any table that only this application creates: its presence with no
# alembic_version row means a pre-Alembic database that needs adopting.
SENTINEL_TABLE = "sources"


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await wait_for_db()
    await apply_schema_migrations()
    await apply_job_queue_schema()

    async with SessionLocal() as session:
        added = await seed_sources(session)
        added_voices = await seed_voices(session)
    if added:
        log.info("Seeded %d sources", added)
    if added_voices:
        log.info("Seeded %d voices", added_voices)
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


async def _scalar(sql: str):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


async def _current_revision() -> str | None:
    if not await _scalar("SELECT to_regclass('alembic_version')"):
        return None
    return await _scalar("SELECT version_num FROM alembic_version")


async def apply_schema_migrations() -> None:
    """Bring the schema to head, adopting a pre-Alembic database if needed."""
    config = alembic_config()
    head = ScriptDirectory.from_config(config).get_current_head()
    current = await _current_revision()
    has_tables = bool(await _scalar(f"SELECT to_regclass('{SENTINEL_TABLE}')"))

    if current is None and has_tables:
        await _adopt_existing_database(config)
        current = ScriptDirectory.from_config(config).get_base()

    if current == head:
        log.info("Schema is at head (%s); nothing to migrate", head)
        return

    if has_tables:
        # Fresh databases hold nothing worth dumping; everything else does.
        await _backup_before_migrating(current, head)

    log.info("Migrating schema %s -> %s", current or "empty database", head)
    await asyncio.to_thread(command.upgrade, config, "head")
    log.info("Schema migrated to %s", head)


async def _adopt_existing_database(config) -> None:
    """Record the baseline for a database built by the pre-Alembic bootstrap.

    Such a database already *has* the baseline schema — the old DDL lists built
    it — so running the baseline migration would fail on every CREATE TABLE. It
    is stamped instead: the version row is written, no DDL runs, and subsequent
    revisions apply normally.

    This fires at most once in the project's life, on the database that existed
    when Alembic landed (2026-07-20), which is why it can assume the base
    revision describes what is already there.
    """
    base = ScriptDirectory.from_config(config).get_base()
    log.warning(
        "Found application tables but no alembic_version: adopting this database "
        "by stamping baseline %s (no DDL will run)", base,
    )
    # Stamping writes a version row and applies no schema changes, so the review
    # gate — which exists to stop unread DDL — has nothing to protect here.
    config.attributes["skip_review_guard"] = True
    await asyncio.to_thread(command.stamp, config, base)
    config.attributes["skip_review_guard"] = False


async def _backup_before_migrating(current: str | None, head: str | None) -> None:
    """Dump the database before schema changes touch it.

    Deliberately fail-closed: if the backup cannot be written, the migration does
    not run and the stack does not start. A failed backup on a night when a
    migration eats a column is precisely the situation this exists to prevent.
    Set `backup_enabled=false` to opt out.
    """
    if not settings.backup_enabled:
        log.warning(
            "backup_enabled=false — migrating %s -> %s with no pre-migration backup",
            current, head,
        )
        return

    from .worker.backup import run_backup

    log.info("Backing up before migrating %s -> %s", current or "empty database", head)
    try:
        dest = await run_backup()
    except Exception as exc:
        raise RuntimeError(
            f"Pre-migration backup failed, so the migration was NOT applied: {exc}. "
            f"Fix the backup target ({settings.backup_dir}) and retry, or set "
            f"backup_enabled=false to migrate without one."
        ) from exc
    log.info("Pre-migration backup written to %s", dest)


async def apply_job_queue_schema() -> None:
    """procrastinate's `schema --apply` is not idempotent — guard on a marker table.

    Kept out of Alembic on purpose: the job-queue schema is procrastinate's to
    own and version, and env.py excludes its tables from autogenerate so the two
    never fight over them.
    """
    if await _scalar("SELECT to_regclass('procrastinate_jobs')"):
        return
    from .worker.app import app as job_app

    async with job_app.open_async():
        await job_app.schema_manager.apply_schema_async()
    log.info("Applied procrastinate schema")


if __name__ == "__main__":
    asyncio.run(main())
