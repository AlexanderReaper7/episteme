"""Scenario planning and execution for one benchmark run.

Everything that decides *what to measure* is here and pure (`plan_items`,
`gate`, `contaminated`); everything that decides *how to talk to llama-server*
is in `client.py`. That split is what makes the interesting half testable
without a GPU, which matters when one real repetition of the longctx scenario is
seven minutes on the fast model and twenty-four on the slow one.

Three rules the executor enforces, each of them a lesson rather than a
preference:

* **Refuse to start on a contended GPU, and label a run that became contended.**
  2026-08-15: Warframe launched mid-run and cumulative prefill slid 37.7 to 32.6
  to 28.6 tok/s inside one prompt. The measurement was not wrong, it was
  unlabelled, which is worse.
* **Hold the interactive lease for the whole run.** A benchmark is somebody
  deliberately using the GPU, so a warden pause must not evict the model
  underneath it. The lease expires in `chat_lease_seconds` (180) and a run is
  minutes to hours, so it is refreshed on a timer rather than taken once.
* **A failing model loses only its own numbers.** Errors land on the sample, not
  on the run, because "the 27B timed out" must not delete the 35B's curves.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..llm.warden import WardenError, warden
from ..models import BenchmarkFixture, BenchmarkRun, BenchmarkSample
from ..worker.control import BENCH_HOLDER, hold_interactive, release_interactive
from .client import BenchClient, BenchError, Cancelled
from .fixtures import synthetic_messages, truncate
from .series import summarize

log = logging.getLogger("episteme.bench.runner")

SCENARIOS = ("quick", "longctx", "ladder", "sweep")


class BenchRefused(Exception):
    """The run was not started, and no row should claim it was. Distinct from a
    failure: nothing ran, so there is nothing to label."""


@dataclass
class Item:
    """One planned measurement. `rep` 0 is the warmup and is stored anyway,
    because "the first repetition is 30 % slow" is itself a fact worth seeing
    once rather than a rule to remember."""

    model: str
    messages: list[dict]
    predict: int
    rep: int = 0
    variant: str = ""
    rung: int | None = None
    warmup: bool = False


@dataclass
class Variant:
    """One point of a `sweep`: a named configuration the warden applies before
    the samples under it are measured."""

    label: str
    sections: dict[str, dict] = field(default_factory=dict)  # preset overrides per model
    extra_args: list[str] = field(default_factory=list)  # appended to llama-server's argv


# --- planning (pure) ---------------------------------------------------------------


def plan_items(
    scenario: str,
    models: list[str],
    params: dict,
    fixture: BenchmarkFixture | None,
) -> list[Item]:
    """Expand a scenario into the ordered list of measurements to take.

    **Order is a measurement decision, not a formatting one.** `--models-max 1`
    means addressing a second model evicts the first, and a model load is 10 to
    15 seconds. Interleaving models would therefore pay for a swap on every
    single sample and fold that cost into whichever phase happened to be running.
    So: all of one model's work, then the next.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario {scenario!r}; known: {list(SCENARIOS)}")
    reps = max(1, int(params.get("reps") or 1))
    predict = max(1, int(params.get("predict") or settings.bench_predict_tokens))
    variants = [Variant(**v) for v in params.get("variants") or []] or [Variant(label="")]

    if scenario in ("longctx", "ladder", "sweep") and fixture is None:
        raise ValueError(f"Scenario {scenario!r} needs a fixture")

    items: list[Item] = []
    for variant in variants:
        for model in models:
            if scenario == "ladder":
                # One cold prefill per rung, and only a token or two of decode:
                # the ladder measures how prefill scales with total prompt
                # length, and generating 256 tokens per rung would spend most of
                # the GPU time on the half not being measured.
                for rung in params.get("rungs") or settings.bench_ladder_rungs:
                    items.append(
                        Item(
                            model=model,
                            messages=truncate(fixture.messages, int(rung), fixture.prompt_tokens),
                            predict=int(params.get("ladder_predict") or 8),
                            rung=int(rung),
                            variant=variant.label,
                        )
                    )
                continue
            messages = (
                synthetic_messages(int(params.get("synthetic_tokens") or 4096))
                if scenario == "quick"
                else list(fixture.messages)
            )
            # The warmup exists because the first request after a load pays for
            # cold caches and, on a swapped model, for the load itself. It is a
            # sample like any other so the cost stays visible, and `warmup` is
            # what keeps it out of the comparison averages.
            for rep in range(reps + (1 if params.get("warmup", True) else 0)):
                items.append(
                    Item(
                        model=model,
                        messages=messages,
                        predict=predict,
                        rep=rep,
                        variant=variant.label,
                        warmup=bool(params.get("warmup", True)) and rep == 0,
                    )
                )
    return items


# The threshold this falls back to when the warden answered `/resources` but not
# `/verdict`. It is the warden's own default, written here as a last resort rather
# than a second opinion: the number that governs is whatever `busy_percent` is
# passed in, and `_busy_percent` reads it from the warden every time (0057).
DEFAULT_BUSY_PERCENT = 25.0


def gate(resources: dict | None, busy_percent: float = DEFAULT_BUSY_PERCENT) -> str | None:
    """Why this run must not start, or None if the card is ours.

    `busy_percent` is the WARDEN's threshold, read from its verdict rather than
    configured here: two numbers meaning "the GPU is busy" would drift, and the
    warden's has been tested against real contention (0024, 0057).

    Stricter than the warden about games, deliberately. The warden treats a
    running game as context and decides on load alone, because a minimised
    launcher is not a reason to stop writing articles. A benchmark is a
    measurement, and one taken next to a game is not wrong so much as
    meaningless.

    A missing warden is not an obstruction. It is optional infrastructure and
    everything degrades to "no opinion" without it, so the run proceeds unsensed
    and `env` records that it was unsensed.
    """
    if not resources:
        return None
    games = resources.get("games_running") or []
    if games:
        return f"a game is running: {', '.join(games)}"
    foreign = resources.get("foreign_gpu_percent")
    if foreign is not None and foreign >= busy_percent:
        return f"foreign GPU load {foreign:.0f}% >= {busy_percent:.0f}%"
    return None


def contaminated(
    env: dict | None, env_end: dict | None, busy_percent: float = DEFAULT_BUSY_PERCENT
) -> bool:
    """Did foreign load appear while we were measuring?

    Asymmetric with `gate` on purpose. Starting requires a quiet card; finishing
    only asks whether anything CHANGED, because a run that was clean throughout
    and one that was clean at both ends but not in between are indistinguishable
    from two samples, and claiming otherwise would be a guess dressed as a flag.
    """
    if not env or not env_end:
        return False
    if bool(env_end.get("games_running")) and not bool(env.get("games_running")):
        return True
    before = env.get("foreign_gpu_percent") or 0
    after = env_end.get("foreign_gpu_percent") or 0
    return after >= busy_percent > before


# --- execution ---------------------------------------------------------------------


class _Lease:
    """Holds the interactive lease for as long as the run lasts.

    A background refresher rather than one long lease, for the same reason the
    lease is a TTL and not a lock (0037): a worker that dies mid-run must not
    strand the GPU for an hour. The TTL is short, the refresher is what keeps it
    alive, and the refresher dying is indistinguishable from the run dying.

    Held and released under `BENCH_HOLDER`, never the bare lease: a chat turn
    ending mid-run must not hand the card back on the benchmark's behalf, which
    it did while the lease was a single shared expiry (0037, 2026-08-16)."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> _Lease:
        await self._refresh()
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        async with SessionLocal() as session:
            await release_interactive(session, BENCH_HOLDER)

    async def _refresh(self) -> None:
        async with SessionLocal() as session:
            await hold_interactive(session, BENCH_HOLDER, seconds=settings.bench_lease_seconds)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(settings.bench_lease_seconds / 3)
            try:
                await self._refresh()
            except Exception as exc:  # a lease refresh must not kill the run
                log.warning("Lease refresh failed: %s", exc)


async def _resources() -> dict | None:
    """`/resources` costs ~3.5s (0023, measured), so this is called at run
    boundaries only, never inside the loop that is being timed."""
    try:
        return await warden.resources()
    except WardenError:
        return None


async def _busy_percent() -> float:
    """The warden's own "the GPU is busy" number. Cheap - `/verdict` is the watch
    thread's cached state, not a new sweep."""
    try:
        verdict = await warden.verdict()
    except WardenError:
        verdict = None
    return ((verdict or {}).get("policy") or {}).get("gpu_busy_percent", DEFAULT_BUSY_PERCENT)


async def _environment() -> tuple[dict | None, float]:
    """A sweep and the threshold to judge it by, read together.

    Together because a warden that could not answer `/resources` will not answer
    `/verdict` either: asking anyway pays a second connect timeout at a run
    boundary, and the answer would be `DEFAULT_BUSY_PERCENT` regardless. No
    sensor, no opinion, and no second wait to arrive at one."""
    env = await _resources()
    if env is None:
        return None, DEFAULT_BUSY_PERCENT
    return env, await _busy_percent()


async def _set_progress(run_id: int, payload: dict) -> None:
    """Live state, overwritten in place. This is the ONLY channel between the
    worker doing the measuring and the web process rendering it: they are
    separate containers with no shared memory, so the run row is the pipe. Same
    trade the log pane makes (0034), the browser is pushed to and only the cheap
    internal leg polls."""
    async with SessionLocal() as session:
        await session.execute(
            update(BenchmarkRun).where(BenchmarkRun.id == run_id).values(progress=payload)
        )
        await session.commit()


async def _cancel_requested(run_id: int) -> bool:
    async with SessionLocal() as session:
        return bool(
            (
                await session.execute(
                    select(BenchmarkRun.cancel_requested).where(BenchmarkRun.id == run_id)
                )
            ).scalar()
        )


async def _load_run(
    session: AsyncSession, run_id: int
) -> tuple[BenchmarkRun, BenchmarkFixture | None]:
    run = await session.get(BenchmarkRun, run_id)
    if run is None:
        raise BenchRefused(f"No benchmark run {run_id}")
    fixture = await session.get(BenchmarkFixture, run.fixture_id) if run.fixture_id else None
    return run, fixture


async def run_benchmark(run_id: int) -> dict:
    """Execute a planned run to completion. Returns a small summary for the job log.

    Structured so that every exit path still stamps the run: a benchmark whose
    row says `running` forever is worse than one that says `failed`, because the
    page treats a running row as live and will keep a progress pane open on it.

    "Every exit path" includes **planning**, which is why `plan_items` is called
    inside the guard rather than above it. It was above it once, and the ordinary
    mistake — a `longctx` launched with the fixture select left on "none" — raised
    before anything could stamp the row, leaving it `queued`, which the page reads
    as live: an unresolvable cancel button and a progress stream that never sends
    `done`. `create_run` now refuses that combination at the form, so this path is
    the second line rather than the first, but it has to hold anyway: the whole
    point of the guard is that it does not enumerate what can go wrong.
    """
    async with SessionLocal() as session:
        run, fixture = await _load_run(session, run_id)
        scenario, models, params = run.scenario, list(run.models), dict(run.params)
        executor = run.executor

    env, busy_percent = await _environment()
    if (reason := gate(env, busy_percent)) is not None:
        async with SessionLocal() as session:
            await session.execute(
                update(BenchmarkRun)
                .where(BenchmarkRun.id == run_id)
                .values(
                    status="failed",
                    error=f"refused: {reason}",
                    env=env,
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()
        raise BenchRefused(reason)

    client = BenchClient()
    status, error = "succeeded", None
    items: list[Item] = []
    done = 0
    variant_applied: str | None = None
    preset_backup: str | None = None

    try:
        items = plan_items(scenario, models, params, fixture)
        async with _Lease():
            build = await client.build()
            async with SessionLocal() as session:
                await session.execute(
                    update(BenchmarkRun)
                    .where(BenchmarkRun.id == run_id)
                    .values(env=env, llama_build=build, status="running")
                )
                await session.commit()

            loaded: set[str] = set()
            for index, item in enumerate(items):
                if await _cancel_requested(run_id):
                    status = "cancelled"
                    break
                if item.variant != variant_applied:
                    # Applying a variant restarts llama-server, so it can only
                    # happen at an item boundary and every model's residency is
                    # gone afterwards.
                    preset_backup = await _apply_variant(params, item.variant, preset_backup)
                    variant_applied = item.variant
                    loaded.clear()
                await _set_progress(
                    run_id,
                    {
                        "item": index,
                        "items": len(items),
                        "model": item.model,
                        "variant": item.variant,
                        "rep": item.rep,
                        "rung": item.rung,
                        "phase": "starting",
                        "processed": 0,
                        "total": 0,
                    },
                )
                await _run_item(client, run_id, item, index, len(items), loaded)
                done += 1

    except Cancelled:
        status = "cancelled"
    except BenchError as exc:
        status, error = "failed", str(exc)
        log.warning("Benchmark run %d failed: %s", run_id, exc)
    except Exception as exc:  # noqa: BLE001 - the row must never be left `running`
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        log.exception("Benchmark run %d crashed", run_id)
    finally:
        if preset_backup:
            # Unconditional: a sweep that failed halfway must not leave the host
            # holding an experimental preset that the next nightly run inherits.
            await _restore_preset(preset_backup)
        await client.aclose()

    env_end, busy_percent = await _environment()
    async with SessionLocal() as session:
        await session.execute(
            update(BenchmarkRun)
            .where(BenchmarkRun.id == run_id)
            .values(
                status=status,
                error=error,
                env_end=env_end,
                contaminated=contaminated(env, env_end, busy_percent),
                finished_at=datetime.now(UTC),
                progress={},
            )
        )
        await session.commit()
    log.info("Benchmark run %d %s: %d/%d samples", run_id, status, done, len(items))
    return {
        "run_id": run_id,
        "status": status,
        "samples": done,
        "planned": len(items),
        "executor": executor,
    }


async def _run_item(
    client: BenchClient,
    run_id: int,
    item: Item,
    index: int,
    total_items: int,
    loaded: set[str],
) -> None:
    """One measurement, one row. Never raises for a model-level failure: the
    error goes on the sample so the other model's numbers survive."""
    was_loaded = item.model in loaded or await client.is_loaded(item.model)

    async def progress(snapshot: dict) -> None:
        await _set_progress(
            run_id,
            {
                "item": index,
                "items": total_items,
                "model": item.model,
                "variant": item.variant,
                "rep": item.rep,
                "rung": item.rung,
                **snapshot,
            },
        )

    sample = BenchmarkSample(
        run_id=run_id,
        model=item.model,
        variant=item.variant,
        rep=item.rep,
        rung=item.rung,
    )
    try:
        series, wall_ms = await client.measure(
            item.model,
            item.messages,
            predict=item.predict,
            should_cancel=lambda: _cancel_requested(run_id),
            on_progress=progress,
        )
    except Cancelled:
        raise
    except BenchError as exc:
        sample.error = str(exc)
    else:
        for key, value in summarize(series, wall_ms).items():
            setattr(sample, key, value)
        # Derived, and only where it is honest to: on the first request after a
        # swap, everything wall time saw that llama.cpp's own timings did not is
        # the load. On a model that was already resident the same subtraction
        # would be HTTP overhead wearing a load's name, so it stays null.
        if not was_loaded:
            sample.load_ms = max(0, sample.wall_ms - sample.prefill_ms - sample.decode_ms)
        loaded.add(item.model)
        sample.args = await client.args_for(item.model)
        resources = await _resources()
        sample.vram_free_mb = (resources or {}).get("vram_free_mb")

    async with SessionLocal() as session:
        session.add(sample)
        await session.commit()


# --- the host executor: sweeps (0041) ----------------------------------------------


async def _apply_variant(params: dict, label: str, backup: str | None) -> str | None:
    """Put one sweep variant in place: edit the preset, restart llama-server.

    Episteme decides WHAT the configuration is and the agent applies it, which is
    the line 0023 actually draws (no policy on the host) and 0041 restates for
    configuration. The backup path is threaded through so the first variant's
    backup, taken against the operator's real preset, is the one restored at the
    end, not the fourth variant's backup of the third variant's edit.
    """
    if not label:
        return backup
    variant = next(
        (Variant(**v) for v in params.get("variants") or [] if v.get("label") == label), None
    )
    if variant is None:
        raise BenchError(f"Sweep variant {label!r} vanished from the run parameters")
    try:
        result = await warden.apply_preset(variant.sections)
        backup = backup or result.get("backup")
        await warden.restart(extra_args=variant.extra_args)
    except WardenError as exc:
        raise BenchError(f"applying variant {label!r} failed: {exc}") from exc
    # llama-server rebinds its port before it can serve, and the router lazily
    # loads on first request, so there is nothing better to wait on than the
    # agent's own port check, which /restart already did.
    return backup


async def _restore_preset(backup: str) -> None:
    try:
        await warden.restore_preset(backup)
        await warden.restart()
    except WardenError as exc:
        # Loud, because the host is now running a configuration nobody chose and
        # the next nightly pipeline run would inherit it.
        log.error("FAILED to restore models-preset.ini from %s: %s", backup, exc)


async def create_run(
    session: AsyncSession,
    *,
    scenario: str,
    models: list[str],
    params: dict,
    fixture_id: int | None = None,
) -> BenchmarkRun:
    """Persist the parameters, then hand the job a single integer.

    The run row IS the parameter record. That is what lets a model list and a
    sweep definition reach the worker at all: `defer_args` validates against four
    integer parameters (`limit`, `story_id`, `post_id`, `source_id`), and a list
    of model names is not one of them. It also means a run can be read back
    afterwards and re-launched exactly.

    **Validation is a dry run of the planner**, not a second list of rules beside
    it. `plan_items` is pure and its result is discarded here; what is wanted is
    its refusals — a missing fixture, an unknown scenario, a rung list that
    expands to nothing — raised while a person is still looking at the form,
    rather than by a worker twenty seconds later against a row nobody is watching.
    Re-deriving those conditions here would give the form and the runner two
    opinions about what is runnable, and they would drift.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario {scenario!r}; known: {list(SCENARIOS)}")
    if not models:
        raise ValueError("Pick at least one model")
    fixture = await session.get(BenchmarkFixture, fixture_id) if fixture_id else None
    if fixture_id and fixture is None:
        raise ValueError(f"No benchmark fixture {fixture_id}")
    plan_items(scenario, models, params, fixture)
    executor = "host" if params.get("variants") else "worker"
    run = BenchmarkRun(
        scenario=scenario,
        models=models,
        params=params,
        fixture_id=fixture_id,
        executor=executor,
        status="queued",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run
