"""Database backups, owned by the worker.

The worker already runs procrastinate, so it owns backups too rather than a
separate service. Each run shells out to `pg_dump` in the PG18 custom format
(`-Fc`) with zstd compression (`--compress=zstd:<level>`) — restore-selective via
`pg_restore`, and matching the zstd-compressed WAL. The worker image ships the
PGDG `postgresql-client-18` so this pg_dump is >= the server and built with zstd.
Dumps land in `settings.backup_dir` (bind-mounted to the host in compose) and old
ones are pruned past `backup_retention_days`.

Manual-only in alpha: triggered via POST /api/jobs/defer/backup_database, no
periodic cron yet (data-loss prevention isn't a priority at this stage). The
`backup_cron` default is kept for when a scheduled task is wired up later.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from ..config import settings
from .app import app

log = logging.getLogger("episteme.worker.backup")

BACKUP_PREFIX = "episteme-"
BACKUP_SUFFIX = ".dump"


def dump_command(url: str, dest: Path) -> list[str]:
    """argv for a PG18 custom-format dump compressed with zstd. The connection
    string (password included) is passed as --dbname, consistent with how the
    rest of the stack passes DATABASE_URL; on this single-user host argv exposure
    to `ps` is acceptable. Pure so the command shape is unit-testable."""
    return [
        "pg_dump",
        "--format=custom",
        f"--compress=zstd:{settings.backup_zstd_level}",
        "--file",
        str(dest),
        "--dbname",
        url,
    ]


def prune_old_backups(directory: Path, retention_days: int) -> int:
    """Delete dumps older than the retention window (by mtime). Returns the count
    removed. retention_days <= 0 disables pruning (keep everything)."""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # a dump vanished or is locked — log, keep going
            log.warning("Could not prune backup %s: %s", path, exc)
    return removed


async def run_backup() -> Path:
    """Produce one compressed dump and prune old ones. Returns the dump path."""
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = directory / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"

    cmd = dump_command(settings.database_url, dest)
    log.info("Starting database backup -> %s (zstd:%d)", dest, settings.backup_zstd_level)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Don't leave a truncated dump on disk masquerading as a good backup.
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"pg_dump failed (exit {proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )

    size_mib = dest.stat().st_size / 1048576
    removed = prune_old_backups(directory, settings.backup_retention_days)
    log.info(
        "Backup complete: %s (%.1f MiB); pruned %d old dump(s)",
        dest.name, size_mib, removed,
    )
    return dest


@app.task(name="episteme.backup_database")
async def backup_database() -> None:
    """Deferred entry point: POST /api/jobs/defer/backup_database.

    Manual-only for now (alpha) — no periodic cron is registered. When data-loss
    prevention matters, add `@app.periodic(cron=settings.backup_cron)` on a thin
    scheduled task that defers this one (see scheduled_pipeline for the pattern)."""
    if not settings.backup_enabled:
        log.info("Backups disabled (backup_enabled=false); skipping")
        return
    await run_backup()
