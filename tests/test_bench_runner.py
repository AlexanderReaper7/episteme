"""The executor's promises, and the run list's poll.

Neither needs a GPU. What is pinned here is not "the benchmark measures
correctly" - `test_bench_series.py` owns that - but the things that go wrong
*around* a measurement: a row left in a status the page reads as live, and a
table that re-swaps itself forever.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

from episteme.bench import runner
from episteme.web.bench import _run_identity, _runs_context
from episteme.web.templating import templates

NOW = datetime.now(UTC)


class _FakeSession:
    """Records every statement and its bound parameters. Enough for code that
    writes through `execute`, which is all of what is exercised here."""

    def __init__(self, recorded: list[tuple[str, dict]]) -> None:
        self.recorded = recorded

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement, params=None):
        compiled = statement.compile()
        self.recorded.append((str(compiled), dict(params or compiled.params)))
        return None

    async def commit(self) -> None:
        pass


class _ExplodingLease:
    """The lease must not be reached in these tests. Entering it would mean the
    failure happened later than the test claims - and the lease is a claim on the
    whole card, which a run that never planned has no business taking."""

    async def __aenter__(self):
        raise AssertionError("the lease was taken for a run that cannot be planned")

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _no_resources() -> None:
    return None


def _updates(recorded: list[tuple[str, dict]], table: str) -> list[dict]:
    return [params for sql, params in recorded if "UPDATE" in sql and table in sql]


# --- what a run boundary asks the warden -------------------------------------------


async def test_an_absent_warden_is_not_asked_twice_at_a_run_boundary(monkeypatch):
    """A run boundary needs two things from llama-warden, the sweep and the
    threshold to read it by, and when the warden is down BOTH are a connect
    timeout. Asking for the second one anyway put 15s of dead wait on each end of
    every run, for an answer that is `DEFAULT_BUSY_PERCENT` either way. `gate`
    has no opinion without a sweep, so there is nothing the number could change.
    """
    from episteme.llm.warden import warden

    async def _explode():
        raise AssertionError("the warden was asked for a verdict it cannot give")

    monkeypatch.setattr(runner, "_resources", _no_resources)
    monkeypatch.setattr(warden, "verdict", _explode)

    assert await runner._environment() == (None, runner.DEFAULT_BUSY_PERCENT)


async def test_the_threshold_that_judges_a_sweep_is_the_wardens(monkeypatch):
    """When there IS a card to judge, the number comes off `/verdict` and not off
    a constant here. One threshold, owned where it is measured (0057)."""

    async def _busy():
        return {"games_running": [], "foreign_gpu_percent": 9.0}

    async def _verdict():
        return {"policy": {"gpu_busy_percent": 5.0}}

    from episteme.llm.warden import warden

    monkeypatch.setattr(runner, "_resources", _busy)
    monkeypatch.setattr(warden, "verdict", _verdict)

    env, busy_percent = await runner._environment()
    assert busy_percent == 5.0
    assert "foreign GPU load" in runner.gate(env, busy_percent)


# --- every exit path stamps the run ------------------------------------------------


@pytest.mark.parametrize("scenario", ["longctx", "ladder", "sweep"])
async def test_a_run_that_cannot_be_planned_is_still_stamped(scenario, monkeypatch):
    """`plan_items` used to be called above the guard, so the ordinary mistake -
    a longctx launched with the fixture select left on "none" - raised before
    anything could stamp the row. `worker/bench.py` catches only `BenchRefused`,
    so the run sat at `queued`, which the page reads as LIVE: a cancel button
    resolving to nothing and a progress stream that never sends `done`."""
    recorded: list[tuple[str, dict]] = []
    run = runner.BenchmarkRun(id=7, scenario=scenario, models=["m"], params={}, executor="worker")

    async def _load(session, run_id):
        return run, None  # the fixture every one of these scenarios needs, absent

    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeSession(recorded))
    monkeypatch.setattr(runner, "_load_run", _load)
    monkeypatch.setattr(runner, "_resources", _no_resources)
    monkeypatch.setattr(runner, "_Lease", _ExplodingLease)

    result = await runner.run_benchmark(7)

    assert result["status"] == "failed"
    final = _updates(recorded, "benchmark_run")[-1]
    assert final["status"] == "failed"
    assert "needs a fixture" in final["error"]
    assert final["finished_at"] is not None


async def test_an_unplannable_run_is_refused_before_a_row_exists():
    """The other half, and the one a person actually meets: `create_run` validates
    by dry-running the planner, so the refusal reaches the launch form as a 422
    rather than a worker twenty seconds later against a row nobody is watching.

    `None` for the session is the assertion: the refusal happens before anything
    is written, so there is nothing for a session to do."""
    with pytest.raises(ValueError, match="needs a fixture"):
        await runner.create_run(None, scenario="longctx", models=["m"], params={})
    with pytest.raises(ValueError, match="Unknown scenario"):
        await runner.create_run(None, scenario="quik", models=["m"], params={})
    with pytest.raises(ValueError, match="at least one model"):
        await runner.create_run(None, scenario="quick", models=[], params={})


# --- the run list is polled with a digest (0033) -----------------------------------


def _row(**over) -> dict:
    row = {
        "id": 12,
        "started_at": NOW - timedelta(minutes=4),
        "finished_at": None,
        "status": "running",
        "scenario": "ladder",
        "executor": "worker",
        "models": ["big"],
        "contaminated": False,
        "error": None,
        "fixture": "write-max",
        "samples": 3,
    }
    return row | over


def test_the_poll_url_carries_the_digest_of_what_is_on_screen():
    """Without it the fragment answered 200 every 10 s forever and re-swapped a
    byte-identical table, which is the churn `_admin_queue.html` exists not to
    do. `hx-target` is named because .admin-main's is otherwise inherited."""
    html = templates.env.get_template("admin/_bench_runs.html").render(**_runs_context([_row()]))
    root = re.search(r'<div id="bench-runs"[^>]*>', html).group(0)
    assert "/admin/benchmarks/partials/runs?v=" in root
    assert 'hx-target="this"' in root
    assert "v={{" not in root and "v=&#34;&#34;" not in root  # a real digest


def test_the_digest_moves_when_a_run_does_and_holds_when_it_does_not():
    """`samples` is the one that matters: the status stays `running` for an hour
    while the count climbs, so a digest without it would freeze the table exactly
    when it is worth watching. `started_at` goes in as an instant rather than as
    "4m ago" - the age is retimed in the browser, so an idle list holds one
    digest instead of moving every minute on its own."""
    base = _runs_context([_row()])["runs_hash"]
    assert _runs_context([_row()])["runs_hash"] == base
    assert _runs_context([_row(samples=4)])["runs_hash"] != base
    assert _runs_context([_row(status="succeeded")])["runs_hash"] != base
    assert _runs_context([_row(contaminated=True)])["runs_hash"] != base
    assert _runs_context([_row(error="read timeout")])["runs_hash"] != base
    assert _runs_context([])["runs_hash"] != base


def test_the_identity_is_the_data_not_the_rendered_row():
    identity = _run_identity(_row())
    assert identity[0] == 12
    assert NOW - timedelta(minutes=4) in identity
