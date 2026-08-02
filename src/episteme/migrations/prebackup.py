"""The dump that must exist before DDL touches real rows.

A migration is the one routine operation that can destroy data faster than it
can be noticed, so a database with anything in it is dumped before any revision
is applied, and the dump is **fail-closed**: if it cannot be written, nothing is
migrated.

**Why this lives here and not in `bootstrap.py`.** It used to live there, and
`bootstrap` is reached only by the compose `migrate` one-shot -- so
`python -m episteme.migrations upgrade` and a bare `alembic upgrade head`, both
of them documented workflows applying DDL to the same database, ran with no dump
at all. This module is called from `env.py` for exactly the reason stated there
about the review gate: env.py is the only code every path into alembic runs
through. A guarantee honored by one of three entry points is not a guarantee.

**pg_dump is a hard requirement, not a nice-to-have.** It ships in the
application image (the Dockerfile installs `postgresql-client-18`, deliberately
>= the server) and is typically absent from the host; `backup_dir` is likewise a
container path that compose bind-mounts to the host. So `upgrade` is a container
operation. Run from the host it stops here with the command to use instead --
which is the whole point of fail-closed: the alternative is migrating
unprotected because the safety net happened to be somewhere else.
"""

from __future__ import annotations

import logging
import shutil

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import settings

log = logging.getLogger("episteme.migrations.backup")

# Any table only this application creates. Its absence means an empty database,
# which holds nothing worth dumping.
SENTINEL_TABLE = "sources"

CONTAINER_HINT = (
    "Run the upgrade where pg_dump and the backup mount actually are:\n"
    "\n"
    "    docker compose run --rm migrate\n"
    "\n"
    "`new`, `status` and `check` are fine from the host -- they apply nothing."
)


def _abort(title: str, *lines: str) -> None:
    """Stop with a framed, readable message rather than a traceback.

    Same treatment `enforce_review_gate` gives its refusals: everything that can
    block a migration is something an operator has to read and act on, so it must
    not arrive looking like a crash."""
    raise SystemExit("\n".join(["", "=" * 78, title, "=" * 78, "", *lines, ""]))


async def _scalar(connection: AsyncConnection, sql: str):
    return (await connection.execute(text(sql))).scalar()


async def _current_revision(connection: AsyncConnection) -> str | None:
    if not await _scalar(connection, "SELECT to_regclass('alembic_version')"):
        return None
    return await _scalar(connection, "SELECT version_num FROM alembic_version")


async def backup_before_migrating(connection: AsyncConnection, *, target: str | None) -> None:
    """Dump the database unless there is demonstrably nothing to protect.

    `target` is the destination revision alembic resolved for this invocation, or
    None when it could not be resolved (a relative `+1`, a downgrade). None means
    "assume this changes something" -- the unknown case has to fall on the side of
    taking a dump, never on the side of skipping one.
    """
    if not await _scalar(connection, f"SELECT to_regclass('{SENTINEL_TABLE}')"):
        return  # empty database: nothing exists yet that a migration could eat

    current = await _current_revision(connection)
    if target is not None and current == target:
        # Alembic is about to do nothing. Dumping here would mean a full pg_dump
        # on every `docker compose up`, which is how a safety measure turns into
        # something people disable.
        return

    if not settings.backup_enabled:
        log.warning(
            "backup_enabled=false -- migrating %s -> %s with NO pre-migration backup",
            current or "empty database",
            target or "the requested revision",
        )
        return

    if shutil.which("pg_dump") is None:
        _abort(
            "MIGRATION BLOCKED -- no pg_dump, so no pre-migration backup",
            f"  Migrating {current or 'empty database'} -> "
            f"{target or 'the requested revision'} would rewrite real data,",
            "  and pg_dump is not on PATH here to capture it first.",
            "",
            CONTAINER_HINT,
            "",
            "  Set backup_enabled=false to migrate without a backup, deliberately.",
        )

    from ..worker.backup import run_backup

    log.info("Backing up before migrating %s -> %s", current or "empty database", target)
    try:
        dest = await run_backup()
    except Exception as exc:
        _abort(
            "MIGRATION BLOCKED -- the pre-migration backup failed",
            f"  {exc}",
            "",
            f"  Nothing was migrated. Fix the backup target ({settings.backup_dir})",
            "  and retry, or set backup_enabled=false to migrate without one.",
            "",
            CONTAINER_HINT,
        )
    log.info("Pre-migration backup written to %s", dest)
