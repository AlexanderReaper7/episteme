from datetime import UTC, datetime
from types import SimpleNamespace

from episteme.worker.maintenance import sweep_stalled_jobs


class FakeManager:
    """Records the retry/prune calls sweep_stalled_jobs makes and lets a test
    fix what get_stalled_jobs returns — no live queue needed."""

    def __init__(self, stalled=(), pruned=()):
        self._stalled = list(stalled)
        self._pruned = list(pruned)
        self.retried: list[tuple[int, datetime]] = []
        self.get_stalled_kwargs: dict = {}
        self.prune_kwargs: dict = {}

    async def get_stalled_jobs(self, *, seconds_since_heartbeat):
        self.get_stalled_kwargs = {"seconds_since_heartbeat": seconds_since_heartbeat}
        return list(self._stalled)

    async def retry_job_by_id_async(self, *, job_id, retry_at):
        self.retried.append((job_id, retry_at))

    async def prune_stalled_workers(self, *, seconds_since_heartbeat):
        self.prune_kwargs = {"seconds_since_heartbeat": seconds_since_heartbeat}
        return list(self._pruned)


def _job(job_id, task_name="episteme.ingest_source"):
    return SimpleNamespace(id=job_id, task_name=task_name)


async def test_sweep_requeues_every_stalled_job():
    now = datetime(2026, 7, 21, 22, 0, tzinfo=UTC)
    manager = FakeManager(stalled=[_job(1165), _job(3180, "episteme.run_pipeline")])

    requeued, pruned = await sweep_stalled_jobs(manager, heartbeat_seconds=60.0, now=now)

    assert requeued == [1165, 3180]
    # Each stalled job is retried immediately (retry_at=now), which is what moves
    # it out of the orphaned `doing` state.
    assert manager.retried == [(1165, now), (3180, now)]


async def test_sweep_passes_threshold_through_and_prunes_dead_workers():
    manager = FakeManager(stalled=[], pruned=[41])

    requeued, pruned = await sweep_stalled_jobs(
        manager, heartbeat_seconds=90.0, now=datetime.now(UTC)
    )

    assert requeued == []
    assert pruned == [41]
    # The same threshold gates both detection and worker pruning.
    assert manager.get_stalled_kwargs == {"seconds_since_heartbeat": 90.0}
    assert manager.prune_kwargs == {"seconds_since_heartbeat": 90.0}


async def test_sweep_is_a_noop_when_nothing_is_stalled():
    manager = FakeManager()
    requeued, pruned = await sweep_stalled_jobs(
        manager, heartbeat_seconds=60.0, now=datetime.now(UTC)
    )
    assert requeued == [] and pruned == []
    assert manager.retried == []
