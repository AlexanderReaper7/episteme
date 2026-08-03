# 0016. Stalled jobs are swept by heartbeat, because procrastinate cannot do it

- Date: 2026-07-21
- Status: accepted
- Rule: a job stuck in `doing` past the heartbeat threshold is requeued, not investigated by hand.

## Context

Procrastinate writes a job's terminal event only when the worker *finishes* it. A redeploy SIGKILL therefore strands the in-flight job in `doing` forever, with `attempts` still 0, and a plain worker has nothing that sweeps these.

This is not hypothetical: `ingest_source` job 1165 stuck on the 2026-07-18 redeploy, worker killed mid-fetch.

## Decision

A periodic `recover_stalled_jobs` in `worker/maintenance.py` (`stalled_job_recovery_cron`, default every 5 minutes) requeues them and prunes the dead workers behind them. Also deferrable by hand at `POST /api/jobs/defer/recover_stalled_jobs`.

Detection is heartbeat-based via `job_manager.get_stalled_jobs`. The live worker beats every 10s, so the 60s `stalled_job_heartbeat_seconds` threshold can never catch a running job. A NULL `worker_id`, meaning a pre-heartbeat orphan, counts as stalled.

## Why requeueing is safe

Ingest is idempotent, and pipeline stages are data-driven: each re-picks whatever rows are unprocessed. Re-running a stage that half-finished costs time, never correctness.

## Consequences

The sweep is why `/admin/jobs` shows `recover_stalled_jobs` constantly, which is part of what drowned that page until retention and folding landed (see 0032).
