"""Job-history retention: forget finished jobs once they stop being worth reading.

procrastinate never deletes a finished job, and nothing in a plain worker does it
for you. That is fine for a queue and ruinous for a history: the two housekeeping
crons write ~1100 rows/day between them against ~5 real pipeline jobs, so within
weeks every "most recent N jobs" view is pure heartbeat. Measured on 2026-08-03,
before this existed: 20 057 rows over 18 days, of which 36 were pipeline stages,
and 42 failed ingest jobs had been sitting unreachable behind a page that only
ever showed the newest 50.

The window is per CLASS, not per age, because age alone cannot express the thing
that matters — a governor tick from last Tuesday is worthless and a pipeline run
from last month is not. `models.job_class` is the single classifier, shared with
the admin queue's fold so the page cannot show a class the prune has already
dropped (or hide one it keeps).

Self-healing (`worker/maintenance.py`) is a separate module on purpose: same
table, opposite job.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..models import (
    FAILURE_STATUSES,
    JOB_CLASS_INGEST,
    JOB_CLASS_MAINTENANCE,
    JOB_CLASS_SCHEDULER,
    JOB_CLASS_WORK,
    LIVE_STATUSES,
    job_class,
)
from .app import app

log = logging.getLogger("episteme.worker")

# FAILURE_STATUSES share one window regardless of class: a failed governor tick is
# worth keeping for as long as a failed write, because both are evidence and
# neither is routine. LIVE_STATUSES are never pruned at any age — `todo` has not
# run and `doing`/`aborting` is running right now.


def prune_cutoffs(
    now: datetime,
    *,
    maintenance_days: int,
    ingest_days: int,
    work_days: int,
    failed_days: int,
) -> dict[str, datetime | None]:
    """Map each job class (plus the pseudo-class `failed`) to the timestamp a job
    must have finished before to be deleted. `None` means keep that class forever
    — any window <= 0 opts out, which is how you disable one tier without
    disabling the prune. Pure, so the policy is testable without a database."""
    days = {
        JOB_CLASS_SCHEDULER: maintenance_days,
        JOB_CLASS_MAINTENANCE: maintenance_days,
        JOB_CLASS_INGEST: ingest_days,
        JOB_CLASS_WORK: work_days,
        "failed": failed_days,
    }
    return {
        name: (now - timedelta(days=value)) if value > 0 else None
        for name, value in days.items()
    }


# A job is old enough when it has no event at or after the cutoff — i.e. nothing
# happened to it since. Expressed as NOT EXISTS rather than max(at) so it rides
# the events(job_id) index instead of grouping the whole table, and paired with a
# positive EXISTS so a job with no events at all is KEPT: absence of history is
# not evidence of age, and every real job gets a `deferred` event from a trigger.
# The events rows go with the job (ON DELETE CASCADE); so does the periodic-defer
# link (procrastinate's own BEFORE DELETE trigger unlinks it).
_DELETE = """
DELETE FROM procrastinate_jobs j
WHERE j.status = ANY(:statuses)
  AND j.status <> ALL(:live)
  {task_filter}
  AND EXISTS (SELECT 1 FROM procrastinate_events e WHERE e.job_id = j.id)
  AND NOT EXISTS (
      SELECT 1 FROM procrastinate_events e
      WHERE e.job_id = j.id AND e.at >= :cutoff
  )
"""


async def _delete_finished(
    session: AsyncSession,
    *,
    statuses: tuple[str, ...],
    cutoff: datetime,
    tasks: list[str] | None,
) -> int:
    """Delete finished jobs in `statuses` whose last event predates `cutoff`.
    `tasks=None` means every task (the failure sweep, which spans all classes)."""
    if tasks is not None and not tasks:
        return 0
    params: dict = {"statuses": list(statuses), "live": list(LIVE_STATUSES), "cutoff": cutoff}
    task_filter = ""
    if tasks is not None:
        task_filter = "AND j.task_name = ANY(:tasks)"
        params["tasks"] = tasks
    result = await session.execute(
        text(_DELETE.format(task_filter=task_filter)), params
    )
    return result.rowcount or 0


async def prune_job_history(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    maintenance_days: int | None = None,
    ingest_days: int | None = None,
    work_days: int | None = None,
    failed_days: int | None = None,
) -> dict[str, int]:
    """Apply the retention policy. Returns rows deleted per class, plus `failed`.

    Successes are swept per class; failures are swept once across every class
    against their own (longer) window. The classification happens in Python over
    the DISTINCT task names actually present, so `models.job_class` stays the ONE
    definition — re-expressing it as a SQL predicate here is exactly the drift
    this arrangement exists to prevent."""
    cutoffs = prune_cutoffs(
        now or datetime.now(UTC),
        maintenance_days=(
            settings.job_history_maintenance_days
            if maintenance_days is None
            else maintenance_days
        ),
        ingest_days=settings.job_history_ingest_days if ingest_days is None else ingest_days,
        work_days=settings.job_history_work_days if work_days is None else work_days,
        failed_days=settings.job_history_failed_days if failed_days is None else failed_days,
    )
    names = (
        (await session.execute(text("SELECT DISTINCT task_name FROM procrastinate_jobs")))
        .scalars()
        .all()
    )
    by_class: dict[str, list[str]] = defaultdict(list)
    for name in names:
        by_class[job_class(name)].append(name)

    deleted: dict[str, int] = {}
    for class_name, tasks in sorted(by_class.items()):
        cutoff = cutoffs.get(class_name)
        if cutoff is None:
            continue
        count = await _delete_finished(
            session, statuses=("succeeded",), cutoff=cutoff, tasks=tasks
        )
        if count:
            deleted[class_name] = count
    if (failed_cutoff := cutoffs["failed"]) is not None:
        count = await _delete_finished(
            session, statuses=FAILURE_STATUSES, cutoff=failed_cutoff, tasks=None
        )
        if count:
            deleted["failed"] = count
    await session.commit()
    return deleted


@app.task(name="episteme.prune_job_history")
async def prune_job_history_task() -> None:
    """Prune once. Independently deferrable
    (POST /api/jobs/defer/prune_job_history) so a queue that has already become
    unreadable can be cleaned up without waiting for the cron."""
    async with SessionLocal() as session:
        deleted = await prune_job_history(session)
    if deleted:
        log.info(
            "Pruned %d job history row(s): %s",
            sum(deleted.values()),
            ", ".join(f"{name}={count}" for name, count in sorted(deleted.items())),
        )


@app.periodic(cron=settings.job_history_prune_cron)
@app.task(name="episteme.scheduled_job_history_prune")
async def scheduled_job_history_prune(timestamp: int) -> None:
    await prune_job_history_task.defer_async()
