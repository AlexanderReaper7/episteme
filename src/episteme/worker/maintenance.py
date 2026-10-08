"""Job-queue self-healing: requeue jobs a dead worker stranded.

procrastinate writes a job's terminal event only when the worker *finishes* it.
A `docker compose up --build` redeploy SIGKILLs the worker container, so any job
it was running is stranded in `doing` forever — a plain `procrastinate ... worker`
has nothing that sweeps these orphans (this is exactly how ingest_source job 1165
stuck on the 2026-07-18 redeploy: worker killed mid-fetch, row left `doing` with
attempts=0 across every later deploy). This module is that missing recovery: a
periodic sweep requeues stalled jobs and prunes the dead workers behind them.

Retention — deleting finished job rows once they stop being worth reading — is
deliberately NOT here: see worker/job_history.py. The two touch the same table
but have opposite jobs (one revives rows, one forgets them) and opposite failure
modes, so they stay independently readable and independently deployable.
"""

import logging
from datetime import UTC, datetime

from ..config import settings
from .app import app

log = logging.getLogger("episteme.worker")


async def sweep_stalled_jobs(
    manager, *, heartbeat_seconds: float, now: datetime
) -> tuple[list[int], list[int]]:
    """Requeue jobs orphaned in `doing` and prune the dead workers behind them.

    A job is stalled if the worker running it stopped heartbeating past
    `heartbeat_seconds`, or it carries no worker_id at all (a pre-heartbeat
    orphan such as job 1165 — procrastinate's heartbeat query counts NULL
    worker_id as stalled). The live worker beats every 10s, so a threshold in
    the tens of seconds cannot catch a job that is actually running. Requeuing
    is safe by design: ingest is idempotent (ON CONFLICT on hash + conditional
    GET) and the pipeline stages are data-driven — they re-pick whatever rows
    are still unprocessed. Returns (requeued_job_ids, pruned_worker_ids).

    Takes the manager as an argument (not `app.job_manager` directly) so the
    logic is unit-testable against a fake without a live queue."""
    stalled = list(await manager.get_stalled_jobs(seconds_since_heartbeat=heartbeat_seconds))
    for job in stalled:
        await manager.retry_job_by_id_async(job_id=job.id, retry_at=now)
        log.warning(
            "Requeued stalled job %d (%s) orphaned by a dead worker",
            job.id,
            job.task_name,
        )
    # Graceful shutdown unregisters a worker; a SIGKILL does not — clear those
    # stale registry rows so procrastinate_workers doesn't accumulate them.
    pruned = list(await manager.prune_stalled_workers(seconds_since_heartbeat=heartbeat_seconds))
    return [job.id for job in stalled], pruned


@app.task(name="episteme.recover_stalled_jobs")
async def recover_stalled_jobs() -> None:
    """Sweep once. Independently deferrable (POST /api/jobs/defer/recover_stalled_jobs)
    so the current orphan can be healed immediately without waiting for the cron."""
    requeued, pruned = await sweep_stalled_jobs(
        app.job_manager,
        heartbeat_seconds=settings.stalled_job_heartbeat_seconds,
        now=datetime.now(UTC),
    )
    if requeued or pruned:
        log.info(
            "Stalled-job sweep: requeued %d job(s) %s, pruned %d dead worker(s)",
            len(requeued),
            requeued,
            len(pruned),
        )


@app.periodic(cron=settings.stalled_job_recovery_cron)
@app.task(name="episteme.scheduled_stalled_recovery")
async def scheduled_stalled_recovery(timestamp: int) -> None:
    await recover_stalled_jobs.defer_async()
