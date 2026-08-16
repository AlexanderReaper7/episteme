"""`/admin/benchmarks` - launch a run, watch it, read it.

HTML routes with no `/api` prefix, like `web/chat.py` and `web/feedback.py`. The
one machine-readable surface is the progress stream, which is
`text/event-stream` for the same reason the log pane is: the interesting part of
a benchmark is the part that has not finished.

**Progress crosses processes through the database.** The measuring happens in the
`worker` container and the watching happens in `web`; they share Postgres and
nothing else. So the runner overwrites `benchmark_run.progress`, this endpoint
polls that one row, and the browser is pushed to - the same asymmetry the log
stream settled on (0034), where the expensive leg is the one nobody polls.

Cancellation is a flag on the same row rather than anything resembling a signal.
The runner reads it between stream chunks and drops the HTTP connection, which is
the only abort llama-server offers; a request that is not streaming has nothing to
notice a disconnect on, which is why `bench/client.py` has no non-streaming path.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import selectinload

from ..bench import report
from ..bench.client import BenchClient, BenchError
from ..bench.fixtures import capture
from ..bench.runner import SCENARIOS, create_run
from ..config import settings
from ..db import SessionLocal
from ..llm.host import host_agent
from ..models import BenchmarkFixture, BenchmarkRun, BenchmarkSample
from .templating import POLL_HEADERS, render, state_hash, templates, unchanged

log = logging.getLogger("episteme.web.bench")

router = APIRouter(prefix="/admin/benchmarks")

LIVE_STATUSES = ("queued", "running")

# Columns the detail page reads. Selected explicitly rather than as ORM objects
# because `report.py` is pure over mappings, and a lazy-load inside a template is
# the failure mode that would put a query in a loop over 40 samples.
_SAMPLE_COLUMNS = (
    BenchmarkSample.id,
    BenchmarkSample.model,
    BenchmarkSample.variant,
    BenchmarkSample.rep,
    BenchmarkSample.rung,
    BenchmarkSample.prompt_n,
    BenchmarkSample.cache_n,
    BenchmarkSample.prefill_ms,
    BenchmarkSample.decode_ms,
    BenchmarkSample.decode_tokens,
    BenchmarkSample.wall_ms,
    BenchmarkSample.load_ms,
    BenchmarkSample.accept_pct,
    BenchmarkSample.vram_free_mb,
    BenchmarkSample.args,
    BenchmarkSample.prefill_series,
    BenchmarkSample.decode_series,
    BenchmarkSample.error,
)


async def _available_models() -> list[str] | None:
    """Model ids the router knows about, or None when llama-server is unreachable.

    None is rendered as a free-text field rather than as an error: the whole point
    of a benchmark form is to be usable while the backend is being restarted into
    a different configuration."""
    client = BenchClient()
    try:
        return [row["id"] for row in await client.models() if row.get("id")]
    except BenchError as exc:
        log.debug("Model list unavailable: %s", exc)
        return None
    finally:
        await client.aclose()


async def _runs(session, limit: int = 30) -> list[dict]:
    rows = await session.execute(
        select(
            BenchmarkRun.id,
            BenchmarkRun.started_at,
            BenchmarkRun.finished_at,
            BenchmarkRun.status,
            BenchmarkRun.scenario,
            BenchmarkRun.executor,
            BenchmarkRun.models,
            BenchmarkRun.contaminated,
            BenchmarkRun.error,
            BenchmarkFixture.name.label("fixture"),
            func.count(BenchmarkSample.id).label("samples"),
        )
        .join(BenchmarkFixture, BenchmarkRun.fixture_id == BenchmarkFixture.id, isouter=True)
        .join(BenchmarkSample, BenchmarkSample.run_id == BenchmarkRun.id, isouter=True)
        .group_by(BenchmarkRun.id, BenchmarkFixture.name)
        .order_by(BenchmarkRun.id.desc())
        .limit(limit)
    )
    return [dict(row._mapping) for row in rows]


def _runs_context(runs: list[dict]) -> dict:
    """The run list plus the digest its own poll carries back (0033).

    Over the row data and not the rendered HTML, and `started_at` is in it as an
    instant rather than as "4m ago": the age is retimed in the browser, so an
    idle page holds one digest for as long as nothing actually moves. Without
    this the fragment re-swapped every 10 s forever, which is what the run list
    was doing until 2026-08-16."""
    return {"runs": runs, "runs_hash": state_hash([_run_identity(run) for run in runs])}


def _run_identity(run: dict) -> tuple:
    """What makes a row look different. `samples` is in it because it is the only
    thing that moves during a long run - the status stays `running` for an hour
    while the count climbs, and a digest that ignored it would freeze the table
    exactly when it is worth watching."""
    return (
        run["id"],
        run["status"],
        run["samples"],
        run["contaminated"],
        run["error"],
        run["started_at"],
        run["finished_at"],
    )


async def _fixtures(session) -> list[BenchmarkFixture]:
    return list(
        (
            await session.execute(
                select(BenchmarkFixture).order_by(BenchmarkFixture.name)
            )
        )
        .scalars()
        .all()
    )


async def _index_context() -> dict:
    async with SessionLocal() as session:
        return {
            "active": "benchmarks",
            **_runs_context(await _runs(session)),
            "fixtures": await _fixtures(session),
            "models": await _available_models(),
            "scenarios": SCENARIOS,
            "agent_enabled": host_agent.enabled,
            "defaults": {
                "predict": settings.bench_predict_tokens,
                "rungs": ", ".join(str(rung) for rung in settings.bench_ladder_rungs),
            },
        }


@router.get("", response_class=HTMLResponse)
async def benchmarks_page(request: Request):
    return render(request, "admin/admin_benchmarks.html", await _index_context())


@router.get("/partials/runs", response_class=HTMLResponse)
async def benchmarks_runs_partial(request: Request, v: str | None = None):
    async with SessionLocal() as session:
        context = _runs_context(await _runs(session))
    if unchanged(context["runs_hash"], v):
        return Response(status_code=204, headers=POLL_HEADERS)
    return _runs_response(request, context)


def _runs_response(request: Request, context: dict):
    """The one place the run list is rendered. The launch, cancel and delete posts
    all answer with it too, so the fragment they swap in carries a current `v` and
    the poll it re-arms is immediately correct."""
    return templates.TemplateResponse(
        request, "admin/_bench_runs.html", context, headers=POLL_HEADERS
    )


@router.get("/partials/fixtures", response_class=HTMLResponse)
async def benchmarks_fixtures_partial(request: Request):
    async with SessionLocal() as session:
        fixtures = await _fixtures(session)
    return templates.TemplateResponse(
        request, "admin/_bench_fixtures.html", {"fixtures": fixtures}
    )


# --- launching ---------------------------------------------------------------------


def _ints(raw: str | None) -> list[int]:
    """A comma or space separated list of positive integers, ignoring the rest.
    Lenient because the field it parses is a text input for ladder rungs, and
    "2048, 4096 8192" is what a person types."""
    out: list[int] = []
    for token in (raw or "").replace(",", " ").split():
        try:
            value = int(token)
        except ValueError:
            continue
        if value > 0:
            out.append(value)
    return out


def _fixture_id(raw: object) -> int | None:
    """The fixture select's value: an id, or empty for "none". Strict, unlike
    `_ints`, because there is exactly one value here and silently dropping an
    unparseable one would launch a fixture-less run whose row claims otherwise."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(text) or None
    except ValueError:
        raise ValueError(f"fixture_id must be a number, got {text!r}") from None


def build_params(form: dict) -> dict:
    """Form fields to the `params` JSON stored on the run.

    Pure, and separate from the handler, because this is the whole contract
    between the page and `plan_items`: a key spelled differently here does not
    fail, it silently produces a run with default settings whose row claims
    otherwise. `tests/test_bench_planning.py` pins the pairing, by feeding this
    function's output straight into `plan_items` rather than by asserting on the
    dict alone - which would pin the spelling to itself.
    """
    params: dict = {
        "reps": max(1, int(form.get("reps") or 1)),
        "predict": max(1, int(form.get("predict") or settings.bench_predict_tokens)),
        "warmup": form.get("warmup") is not None,
    }
    scenario = form.get("scenario")
    if scenario == "quick":
        params["synthetic_tokens"] = max(1, int(form.get("synthetic_tokens") or 4096))
    if scenario == "ladder":
        params["rungs"] = _ints(form.get("rungs")) or list(settings.bench_ladder_rungs)
        params["ladder_predict"] = max(1, int(form.get("ladder_predict") or 8))
    if scenario == "sweep":
        raw = (form.get("variants") or "").strip()
        try:
            variants = json.loads(raw) if raw else []
        except json.JSONDecodeError as exc:
            raise ValueError(f"variants is not valid JSON: {exc}") from exc
        if not isinstance(variants, list) or not variants:
            raise ValueError("A sweep needs a non-empty JSON list of variants")
        for variant in variants:
            if not isinstance(variant, dict) or not variant.get("label"):
                raise ValueError("Every sweep variant needs a label")
            stray = set(variant) - {"label", "sections", "extra_args"}
            if stray:
                raise ValueError(f"Unknown variant keys: {sorted(stray)}")
        params["variants"] = variants
    return params


@router.post("/run", response_class=HTMLResponse)
async def benchmarks_run(request: Request):
    form = await request.form()
    scenario = str(form.get("scenario") or "quick")
    models = [str(value) for value in form.getlist("models") if str(value).strip()]
    if not models:
        models = [
            name.strip() for name in str(form.get("models_text") or "").split(",") if name.strip()
        ]
    # Inside the guard, not above it. Every field on this form is a string typed
    # or posted by a browser, so every parse of one is a 422 waiting to happen;
    # `fixture_id` sat outside and turned a mistyped value into a 500 with nothing
    # on the page to say what was wrong.
    try:
        fixture_id = _fixture_id(form.get("fixture_id"))
        params = build_params(dict(form))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if scenario == "sweep" and not host_agent.enabled:
        raise HTTPException(
            422, "A sweep restarts llama-server, which needs the host agent (LLM_HOST_AGENT_URL)"
        )

    async with SessionLocal() as session:
        try:
            run = await create_run(
                session,
                scenario=scenario,
                models=models,
                params=params,
                fixture_id=fixture_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        run_id = run.id

    from ..worker.app import app as job_app

    async with job_app.open_async():
        await job_app.configure_task("episteme.bench_run").defer_async(run_id=run_id)
    log.info("Deferred benchmark run %d (%s, %s)", run_id, scenario, ", ".join(models))
    return await benchmarks_runs_partial(request)


@router.post("/{run_id}/cancel", response_class=HTMLResponse)
async def benchmarks_cancel(request: Request, run_id: int):
    """Ask the runner to stop. A request, not a kill: it takes effect at the next
    stream chunk, so a model still loading will finish loading first."""
    async with SessionLocal() as session:
        result = await session.execute(
            update(BenchmarkRun)
            .where(BenchmarkRun.id == run_id, BenchmarkRun.status.in_(LIVE_STATUSES))
            .values(cancel_requested=True)
        )
        await session.commit()
        if not result.rowcount:
            raise HTTPException(409, f"Run {run_id} is not running")
        context = _runs_context(await _runs(session))
    return _runs_response(request, context)


@router.post("/{run_id}/delete", response_class=HTMLResponse)
async def benchmarks_delete(request: Request, run_id: int):
    async with SessionLocal() as session:
        run = await session.get(BenchmarkRun, run_id)
        if run is None:
            raise HTTPException(404, f"No benchmark run {run_id}")
        if run.status in LIVE_STATUSES:
            raise HTTPException(409, "Cancel the run before deleting it")
        await session.delete(run)  # samples cascade
        await session.commit()
        context = _runs_context(await _runs(session))
    return _runs_response(request, context)


# --- fixtures ----------------------------------------------------------------------


@router.post("/fixtures/capture", response_class=HTMLResponse)
async def benchmarks_capture(request: Request):
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "A fixture needs a name")
    try:
        percentile = float(form.get("percentile") or 1.0)
    except ValueError as exc:
        raise HTTPException(422, "percentile must be a number between 0 and 1") from exc
    async with SessionLocal() as session:
        try:
            await capture(
                session,
                name=name,
                stage=str(form.get("stage") or "write"),
                percentile=min(1.0, max(0.0, percentile)),
                chain_id=str(form.get("chain_id") or "").strip() or None,
                notes=str(form.get("notes") or "").strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return await benchmarks_fixtures_partial(request)


@router.post("/fixtures/{fixture_id}/delete", response_class=HTMLResponse)
async def benchmarks_fixture_delete(request: Request, fixture_id: int):
    """Runs that used it keep their numbers; `fixture_id` goes NULL (the FK is
    `ON DELETE SET NULL`) and the run row still records the models, the params
    and the measured `prompt_n` of every sample."""
    async with SessionLocal() as session:
        await session.execute(
            delete(BenchmarkFixture).where(BenchmarkFixture.id == fixture_id)
        )
        await session.commit()
    return await benchmarks_fixtures_partial(request)


# --- one run -----------------------------------------------------------------------


@router.get("/{run_id}", response_class=HTMLResponse)
async def benchmark_detail(request: Request, run_id: int):
    async with SessionLocal() as session:
        run = (
            await session.execute(
                select(BenchmarkRun)
                .options(selectinload(BenchmarkRun.fixture))
                .where(BenchmarkRun.id == run_id)
            )
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(404, f"No benchmark run {run_id}")
        samples = [
            dict(row._mapping)
            for row in await session.execute(
                select(*_SAMPLE_COLUMNS)
                .where(BenchmarkSample.run_id == run_id)
                .order_by(BenchmarkSample.id)
            )
        ]
    params = dict(run.params or {})
    return render(
        request,
        "admin/admin_benchmark_run.html",
        {
            "active": "benchmarks",
            "run": run,
            "samples": samples,
            "rows": report.compare_rows(params, samples),
            "charts": report.run_charts(run.scenario, params, samples),
            "is_warmup": lambda sample: report.is_warmup(params, sample),
            "live": run.status in LIVE_STATUSES,
        },
    )


@router.get("/{run_id}/progress")
async def benchmark_progress(request: Request, run_id: int, poll: float = Query(1.0, ge=0.2)):
    """Live position of a run, as server-sent events.

    Polls one indexed row rather than holding anything open across the container
    boundary, for the same reason the log stream does: the worker must not have a
    subscriber to notice, and a run that outlives a `docker compose up -d web`
    has to keep reporting afterwards.

    Two events. `progress` carries the runner's own payload verbatim (item index,
    model, phase, tokens processed); `done` is sent once when the row leaves a
    live status, which is what tells the page to reload itself into the finished
    view instead of holding an empty progress bar forever.
    """
    interval = max(poll, settings.llm_log_stream_interval_seconds)

    async def events():
        last: str | None = None
        silent = 0.0
        while not await request.is_disconnected():
            async with SessionLocal() as session:
                row = (
                    await session.execute(
                        select(
                            BenchmarkRun.status,
                            BenchmarkRun.progress,
                            BenchmarkRun.cancel_requested,
                        ).where(BenchmarkRun.id == run_id)
                    )
                ).first()
            if row is None:
                yield _sse("done", {"status": "missing"})
                return
            status, progress, cancel_requested = row
            payload = {
                "status": status,
                "cancel_requested": bool(cancel_requested),
                **(progress or {}),
            }
            frame = json.dumps(payload, sort_keys=True)
            if frame != last:
                yield _sse("progress", payload)
                last = frame
                silent = 0.0
            if status not in LIVE_STATUSES:
                yield _sse("done", {"status": status})
                return
            silent += interval
            if silent >= 15:
                yield ": keep-alive\n\n"
                silent = 0.0
            await asyncio.sleep(interval)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"
