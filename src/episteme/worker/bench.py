"""The benchmark job.

One task taking one integer, because the run row already holds every parameter
(`bench/runner.create_run`). The alternative, passing a model list and a sweep
definition as job arguments, would have needed a second validation path beside
`defer_args` and would have left no record of what a finished run was asked to do.

`retry=0` deliberately. Retrying an aborted benchmark is never right: a run that
failed because a game started must not silently restart into the same
contention, a cancelled one was cancelled on purpose, and a partially completed
run's samples are already stored and would be duplicated by a second attempt.
"""

import logging

from ..bench.runner import BenchRefused, run_benchmark
from .app import app

log = logging.getLogger("episteme.worker.bench")


@app.task(name="episteme.bench_run")
async def bench_run(run_id: int) -> None:
    try:
        result = await run_benchmark(run_id)
    except BenchRefused as exc:
        # The run row already carries the refusal and its reason, so this is a
        # clean end rather than a failure: raising would put a red row in the job
        # queue for a decision the system made correctly.
        log.info("Benchmark run %d refused: %s", run_id, exc)
        return
    log.info("Benchmark run %d finished: %s", run_id, result)
