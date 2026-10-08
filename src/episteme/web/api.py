"""JSON API under /api — the scriptable face of the app.

Single-user, no auth (decided constraint). The admin UI renders the same data
as HTML; scripts and future integrations use these endpoints. Serializers are
plain dict builders — the ORM models are the schema."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from croniter import croniter
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import and_, func, or_, select, text, update

from ..config import settings
from ..db import SessionLocal
from ..llm import gateway
from ..llm.warden import WardenError, warden
from ..models import (
    JOB_CLASS_INGEST,
    JOB_CLASS_MAINTENANCE,
    JOB_CLASS_SCHEDULER,
    JOB_CLASS_WORK,
    JOB_PLUMBING_CLASSES,
    LIVE_STATUSES,
    POST_SCOPED_STAGES,
    Feedback,
    InterestProfile,
    LlmCall,
    PipelineRun,
    Post,
    PostAudio,
    Source,
    SourceItem,
    Story,
    job_class,
)
from ..recommend import feedback, profile, topics
from ..recommend.scoring import defer_rescore
from ..recommend.search import search_posts
from ..tts import (
    build_script,
    default_voice_id,
    get_voice,
    media_type_for,
    script_hash,
    tee_to_client,
)
from ..tts.store import upsert_post_audio

router = APIRouter(prefix="/api")

# Tasks the defer endpoint may enqueue (name → procrastinate task, allowed params)
# — everything else is 404. The single-stage entries all map to
# episteme.pipeline_stage; their allowed params mirror worker.pipeline.STAGE_PARAMS.
DEFERRABLE_TASKS: dict[str, tuple[str, frozenset[str]]] = {
    "ingest_all": ("episteme.ingest_all", frozenset()),
    "ingest_source": ("episteme.ingest_source", frozenset({"source_id"})),
    "run_pipeline": ("episteme.run_pipeline", frozenset()),
    "backup_database": ("episteme.backup_database", frozenset()),
    "recover_stalled_jobs": ("episteme.recover_stalled_jobs", frozenset()),
    "prune_job_history": ("episteme.prune_job_history", frozenset()),
    # Vocabulary bootstrap, two-phase on purpose: propose writes a reviewable
    # proposal, apply commits it (see recommend/topics.py).
    "propose_topics": ("episteme.propose_topics", frozenset()),
    "apply_topics": ("episteme.apply_topics", frozenset()),
    # Normally automatic (the embed stage heals what an outage left behind); this
    # is the lever for not waiting for the next run.
    "backfill_topic_embeddings": ("episteme.backfill_topic_embeddings", frozenset()),
    # The morning digest and the lunch push (0056). Both run on their own cron;
    # these are the levers for sending one now, which is also how a new ntfy
    # server gets checked without waiting for tomorrow.
    "send_digest": ("episteme.send_digest", frozenset()),
    "matsedel_notify": ("episteme.matsedel_notify", frozenset()),
    "embed": ("episteme.pipeline_stage", frozenset({"limit"})),
    "cluster": ("episteme.pipeline_stage", frozenset({"limit"})),
    "triage": ("episteme.pipeline_stage", frozenset({"limit", "story_id"})),
    "write": ("episteme.pipeline_stage", frozenset({"limit", "story_id"})),
    "qa": ("episteme.pipeline_stage", frozenset({"limit", "post_id"})),
    "summarize": ("episteme.pipeline_stage", frozenset({"limit", "post_id"})),
    "narrate": ("episteme.pipeline_stage", frozenset({"limit", "post_id"})),
    "score": ("episteme.pipeline_stage", frozenset({"limit", "post_id"})),
    # A correspondent's own schedule (0046). Every weekday by cron (0054); this
    # is the lever for reading the kitchens now rather than waiting for morning.
    "matsedel_scrape": ("episteme.matsedel_scrape", frozenset()),
}


def defer_args(task: str, **params: int | None) -> tuple[str, dict]:
    """Validate a defer request; returns (procrastinate task name, job kwargs).
    Raises KeyError for unknown tasks, ValueError for bad params — the endpoint
    maps those to 404/422. Pure so it's unit-testable without a queue."""
    try:
        task_name, allowed = DEFERRABLE_TASKS[task]
    except KeyError:
        raise KeyError(f"Unknown task {task!r}; deferrable: {sorted(DEFERRABLE_TASKS)}") from None
    provided = {key: value for key, value in params.items() if value is not None}
    if stray := set(provided) - set(allowed):
        raise ValueError(
            f"{task} does not accept {sorted(stray)}; allowed: {sorted(allowed) or 'none'}"
        )
    if task == "ingest_source" and "source_id" not in provided:
        raise ValueError("ingest_source requires source_id")
    if task_name == "episteme.pipeline_stage":
        provided["stage"] = task
    return task_name, provided


# --- Serializers ------------------------------------------------------------------


def _favicon_url(feed_url: str | None) -> str | None:
    """A source's favicon, derived from its feed host. Hotlinked from DuckDuckGo's
    icon service (returns a globe fallback for hosts with no icon), matching the
    project's hotlink-and-degrade policy — the <img> onerror hides a broken one.

    Feed hosts are often subdomains (feeds.arstechnica.com) whose favicon lives on
    the registrable domain, so we strip common feed/www subdomains down to the last
    two labels — good enough for the plain domains our sources use."""
    if not feed_url:
        return None
    host = urlsplit(feed_url).netloc.split(":", 1)[0]  # drop any :port
    if not host:
        return None
    labels = host.split(".")
    if len(labels) > 2 and labels[0] in ("feeds", "feed", "www", "rss", "blog", "news"):
        host = ".".join(labels[1:])
    return f"https://icons.duckduckgo.com/ip3/{host}.ico"


def _source_dict(source: Source) -> dict:
    config = source.config or {}
    return {
        "id": source.id,
        "name": source.name,
        "type": source.type_name,
        "enabled": source.enabled,
        "http_mode": config.get("http_mode", "polite"),
        "feed_url": config.get("feed_url"),
        "favicon_url": _favicon_url(config.get("feed_url")),
        "credibility_rating": source.credibility_rating,
        # Config keys beyond the ones that already have dedicated columns —
        # per-source knobs like min_request_gap_seconds / impersonate_profile.
        "config_extra": {k: v for k, v in config.items() if k not in ("feed_url", "http_mode")},
        "cooldown_until": source.cooldown_until,
        "last_fetched_at": source.last_fetched_at,
    }


def _story_dict(story: Story) -> dict:
    return {
        "id": story.id,
        "status": story.status,
        "triage_decision": story.triage_decision,
        "triage_reason": story.triage_reason,
        "rank_score": story.rank_score,
        "topics": story.topics,
        "item_count": story.item_count,
        "first_item_at": story.first_item_at,
        "last_item_at": story.last_item_at,
        "research_notes": story.research_notes,
    }


def _item_dict(item: SourceItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "published_at": item.published_at,
        "source": item.source.name if item.source else None,
    }


def _post_dict(post: Post, with_sections: bool = False) -> dict:
    data = {
        "id": post.id,
        "story_id": post.story_id,
        "kind": post.kind,
        "title": post.title,
        "summary": post.summary,
        "difficulty": post.difficulty,
        "topics": post.topics,
        "reading_time_minutes": post.reading_time_minutes,
        "model_used": post.model_used,
        "quality_score": post.quality_score,
        "status": post.status,
        "generated_at": post.generated_at,
        "archived_at": post.archived_at,
        "pinned": post.pinned,
    }
    if with_sections:
        data["sections"] = post.sections
    return data


def _run_dict(run: PipelineRun) -> dict:
    return {
        "id": run.id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "stages": run.stages,
        "error": run.error,
    }


def _llm_call_dict(call: LlmCall, full: bool = False) -> dict:
    data = {
        "id": call.id,
        "created_at": call.created_at,
        "role": call.role,
        "model": call.model,
        "kind": call.kind,
        "stage": call.stage,
        "story_id": call.story_id,
        "post_id": call.post_id,
        "attempt_id": call.attempt_id,
        "pinned": call.pinned,
        "chain_id": call.chain_id,
        "seq": call.seq,
        "duration_ms": call.duration_ms,
        "prompt_tokens": call.prompt_tokens,
        "completion_tokens": call.completion_tokens,
        "error": call.error,
    }
    if full:
        data["request"] = call.request
        data["response"] = call.response
    return data


# --- Status -----------------------------------------------------------------------


async def llm_endpoint_view() -> dict:
    """Health, inventory and the role table for every distinct endpoint.

    One probe per endpoint answers health AND inventory, so the model list is
    whatever every configured server reports — not just the default URL's, which
    is only one of them once a role is moved elsewhere.

    Its own function because two surfaces need exactly this and nothing else
    around it: `/api/status`, and the admin dashboard's endpoint card, which is
    re-rendered on the backend panel's poll (`admin.backend_context`). Copying
    the four lines into the second caller is how the two views start disagreeing
    about what an endpoint is."""
    endpoints = await gateway.endpoint_status()
    return {
        "endpoints": endpoints,
        "models": [
            dict(model, endpoint=endpoint["url"])
            for endpoint in endpoints
            for model in endpoint["models"]
        ],
        "roles": gateway.role_config(),
    }


@router.get("/status")
async def api_status():
    from ..worker.control import pause_state

    async with SessionLocal() as session:
        pause = await pause_state(session)
        story_counts = dict(
            (await session.execute(select(Story.status, func.count()).group_by(Story.status))).all()
        )
        item_total = (
            await session.execute(select(func.count()).select_from(SourceItem))
        ).scalar_one()
        unembedded = (
            await session.execute(
                select(func.count()).select_from(SourceItem).where(SourceItem.embedding.is_(None))
            )
        ).scalar_one()
        # Generated content only — identity-only aggregate cards would drown the
        # "what did the writer produce" signal this stat exists for.
        post_total = (
            await session.execute(
                select(func.count()).select_from(Post).where(Post.kind != "aggregate")
            )
        ).scalar_one()
        last_run = (
            await session.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1))
        ).scalar_one_or_none()

    llm = await llm_endpoint_view()

    return {
        "llm": {
            "base_url": settings.llm_base_url,
            "available": all(e["available"] for e in llm["endpoints"]),
            **llm,
            # Lifecycle/process facts, not inference facts — and cheap: the agent
            # skips its subprocess entirely when nothing is listening. `/resources`
            # is deliberately NOT here; it costs ~3.5s and has its own route.
            "warden": {
                "enabled": warden.enabled,
                "url": settings.llm_warden_url,
                "status": await warden.status(),
            },
        },
        # `reason` is what lets the UI distinguish "you paused this" from "the
        # warden paused this because the GPU was busy".
        "pipeline": pause,
        "stories": story_counts,
        "source_items": {"total": item_total, "unembedded": unembedded},
        "posts": post_total,
        "last_run": _run_dict(last_run) if last_run else None,
    }


@router.get("/sources")
async def api_sources():
    async with SessionLocal() as session:
        sources = (await session.execute(select(Source).order_by(Source.name))).scalars().all()
        counts = dict(
            (
                await session.execute(
                    select(SourceItem.source_id, func.count()).group_by(SourceItem.source_id)
                )
            ).all()
        )
    return [{**_source_dict(s), "item_count": counts.get(s.id, 0)} for s in sources]


@router.get("/sources/stats")
async def api_sources_stats():
    """Aggregate source health for the Sources page: counts, the single most
    recently fetched source, and only the cooldowns that are still in effect
    (a past cooldown_until is expired — never surface it as active)."""
    sources = await api_sources()
    now = datetime.now(UTC)
    fetched = [s for s in sources if s["last_fetched_at"]]
    most_recent = max(fetched, key=lambda s: s["last_fetched_at"]) if fetched else None
    active_cooldowns = [s for s in sources if s["cooldown_until"] and s["cooldown_until"] > now]
    return {
        "source_count": len(sources),
        "enabled_count": sum(1 for s in sources if s["enabled"]),
        "item_count": sum(s["item_count"] for s in sources),
        "most_recent": most_recent,
        "active_cooldowns": active_cooldowns,
        "now": now,
    }


# --- Pipeline runs ----------------------------------------------------------------


def next_cron_fire(expr: str) -> datetime | None:
    """Next fire time of a cron expression from now (UTC), or None if unparseable.
    Pipeline/ingest runs have no queued row — their 'next' is just the schedule."""
    try:
        return croniter(expr, datetime.now(UTC)).get_next(datetime)
    except (ValueError, KeyError):
        return None


@router.get("/runs")
async def api_runs(limit: Annotated[int, Query(ge=1, le=200)] = 20):
    async with SessionLocal() as session:
        runs = (
            (
                await session.execute(
                    select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [_run_dict(r) for r in runs]


@router.get("/runs/summary")
async def api_runs_summary():
    """Condensed pipeline-run state for the dashboard: whatever is running now,
    the last finished run, and the next scheduled fire time (nightly cron)."""
    async with SessionLocal() as session:
        running = (
            (
                await session.execute(
                    select(PipelineRun)
                    .where(PipelineRun.status == "running")
                    .order_by(PipelineRun.id.desc())
                )
            )
            .scalars()
            .all()
        )
        last = (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.status != "running")
                .order_by(PipelineRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return {
        "running": [_run_dict(r) for r in running],
        "last": _run_dict(last) if last else None,
        "next_fire": next_cron_fire(settings.pipeline_cron),
    }


# --- Job queue --------------------------------------------------------------------

# What a queue row means, derived rather than displayed raw. Three things were
# unreadable before: every pipeline stage was the same task name
# (`episteme.pipeline_stage`) with the interesting word hidden in the args; the
# periodic entry points carried a raw unix timestamp as their only visible
# detail; and there was no time anywhere, because `scheduled_at` is NULL for an
# immediately-deferred job — which is nearly all of them.

# args worth showing next to the label, in this order. `stage` is excluded: it
# IS the label. `timestamp` is excluded because it is procrastinate's own cron
# bookkeeping — a unix integer that reads as data and says nothing.
_JOB_DETAIL_ARGS = ("source_id", "story_id", "post_id", "voice", "limit")
_JOB_DETAIL_LABELS = {"source_id": "source", "story_id": "story", "post_id": "post"}


def job_presentation(row: dict) -> dict:
    """Row + the derived fields the queue view reads: `label` (what ran),
    `detail` (which target), `job_class`, and `duration_seconds`.

    A `pipeline_stage` row is labelled by its stage, since that is the only thing
    distinguishing one from another; everything else drops the `episteme.` prefix
    that is identical on every row and therefore carries no information."""
    args = row.get("args") or {}
    task = row["task_name"]
    label = args.get("stage") if task == "episteme.pipeline_stage" else None
    label = label or task.removeprefix("episteme.")
    detail = " · ".join(
        f"{_JOB_DETAIL_LABELS.get(key, key)} {args[key]}"
        for key in _JOB_DETAIL_ARGS
        if args.get(key) is not None
    )
    started, finished = row.get("started"), row.get("finished")
    return {
        **row,
        "label": label,
        "detail": detail,
        "job_class": job_class(task),
        "duration_seconds": (
            (finished - started).total_seconds() if started and finished else None
        ),
    }


# One lateral pass over a job's events gives both timestamps. procrastinate keeps
# no timing on the job row itself — `scheduled_at` is the cron's intent, NULL for
# anything deferred on demand — so without this join the queue can only say that
# something happened, never when or for how long.
_JOB_SELECT = """
SELECT j.id, j.task_name, j.status, j.args, j.attempts, j.scheduled_at,
       e.started, e.finished
FROM procrastinate_jobs j
LEFT JOIN LATERAL (
    SELECT max(at) FILTER (WHERE type = 'started') AS started,
           max(at) FILTER (WHERE type NOT IN ('started', 'deferred', 'scheduled'))
               AS finished
    FROM procrastinate_events WHERE job_id = j.id
) e ON true
"""


@router.get("/jobs")
async def api_jobs(
    status: str | None = None,
    # Annotated, NOT `= Query(None, alias="class")`. The admin routes call these
    # handlers directly as plain async functions (see web/admin.py), and a
    # `Query(...)` default is a Query OBJECT when FastAPI is not the one filling
    # it in — truthy, so `if job_class_:` fired on every direct call and filtered
    # the queue against a sentinel no job could match. Annotated keeps the real
    # default a real None.
    job_class_: Annotated[str | None, Query(alias="class")] = None,
    exclude_plumbing: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
):
    """Recent jobs, newest first. `class` filters to one of scheduler/maintenance/
    ingest/work; `exclude_plumbing` drops the two self-firing housekeeping classes
    in one go, which is what the queue page's default view asks for.

    Class filtering happens in Python (`models.job_class` is prefix-matched, and
    duplicating it as a SQL predicate is precisely the drift the shared classifier
    exists to prevent), so a filtered query over-fetches and trims. The multiplier
    is sized for the real ratio — plumbing outnumbers work ~200:1 — and the cap
    keeps a pathological queue from being read whole."""
    where = ["true"]
    params: dict = {"limit": limit}
    if status:
        where.append("j.status = :status")
        params["status"] = status
    wanted: tuple[str, ...] | None = None
    if job_class_:
        wanted = (job_class_,)
    elif exclude_plumbing:
        wanted = tuple(
            c
            for c in (JOB_CLASS_SCHEDULER, JOB_CLASS_MAINTENANCE, JOB_CLASS_INGEST, JOB_CLASS_WORK)
            if c not in JOB_PLUMBING_CLASSES
        )
    params["limit"] = limit if wanted is None else min(limit * 40, 4000)
    query = _JOB_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY j.id DESC LIMIT :limit"
    async with SessionLocal() as session:
        rows = (await session.execute(text(query), params)).mappings().all()
    jobs = [job_presentation(dict(row)) for row in rows]
    if wanted is not None:
        jobs = [job for job in jobs if job["job_class"] in wanted]
    return jobs[:limit]


@router.get("/jobs/summary")
async def api_jobs_summary():
    """Condensed queue state for the dashboard: what's running now, what's next
    in line (earliest scheduled todo), and the most recently finished job. Rows
    carry the same derived label/timing as the queue page — the dashboard used to
    print `episteme.pipeline_stage` here, which names the mechanism and not the
    work."""
    async with SessionLocal() as session:
        running = (
            (
                await session.execute(
                    text(f"{_JOB_SELECT} WHERE j.status = 'doing' ORDER BY j.id DESC LIMIT 5")
                )
            )
            .mappings()
            .all()
        )
        upcoming = (
            (
                await session.execute(
                    text(
                        f"{_JOB_SELECT} WHERE j.status = 'todo' "
                        "ORDER BY j.scheduled_at ASC NULLS FIRST, j.id ASC LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
        recent = (
            (
                await session.execute(
                    text(f"{_JOB_SELECT} WHERE j.status <> ALL(:live) ORDER BY j.id DESC LIMIT 1"),
                    {"live": list(LIVE_STATUSES)},
                )
            )
            .mappings()
            .first()
        )
    return {
        "running": [job_presentation(dict(r)) for r in running],
        "upcoming": job_presentation(dict(upcoming)) if upcoming else None,
        "recent": job_presentation(dict(recent)) if recent else None,
    }


# The models a target id names. Keys are the param names in DEFERRABLE_TASKS, so
# adding a target to a task without teaching this mapping about it is a KeyError
# at defer time rather than a silent pass - which is the whole failure this exists
# to end.
TARGET_MODELS = {"story_id": Story, "post_id": Post, "source_id": Source}


async def _check_targets(session, **ids: int | None) -> None:
    """Refuse a target id that names no row, with a 422 saying which one.

    `defer_args` is deliberately pure (unit-testable with no database), so it can
    validate that a task ACCEPTS `post_id` but never that post 999 exists. The
    result was a job enqueued happily, run twenty minutes later, and doing
    nothing - the same silence as a dropped parameter, one stage further on.

    Ids that are None are simply absent and must not be checked.
    """
    for name, ident in ids.items():
        if ident is None:
            continue
        # Indexed, not .get(): a target added to DEFERRABLE_TASKS without a model
        # here must be a KeyError, not a silent pass. See TARGET_MODELS.
        if await session.get(TARGET_MODELS[name], ident) is None:
            # Raise on the first one. Two wrong ids is still one mistake to fix,
            # and a message naming both reads as two separate failures.
            raise HTTPException(422, f"No {name.removesuffix('_id')} with id {ident}")


@router.post("/jobs/defer/{task}")
async def api_defer(
    task: str,
    # Annotated so the Python defaults stay real Nones for admin.py's direct
    # calls — see the note on api_jobs. Safe here only by accident today (the
    # admin route passes all four explicitly), which is exactly the kind of
    # accident that stops being true on the next edit.
    limit: Annotated[int | None, Query(ge=1)] = None,
    story_id: Annotated[int | None, Query(ge=1)] = None,
    post_id: Annotated[int | None, Query(ge=1)] = None,
    source_id: Annotated[int | None, Query(ge=1)] = None,
):
    """Enqueue a job. Single pipeline stages take caps/targets, e.g.
    POST /api/jobs/defer/write?limit=2  or  /api/jobs/defer/qa?post_id=8."""
    try:
        task_name, kwargs = defer_args(
            task, limit=limit, story_id=story_id, post_id=post_id, source_id=source_id
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0]))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    async with SessionLocal() as session:
        await _check_targets(session, story_id=story_id, post_id=post_id, source_id=source_id)
    from ..worker.app import app as job_app

    async with job_app.open_async():
        job_id = await job_app.configure_task(task_name).defer_async(**kwargs)
    return {"deferred": task_name, "job_id": job_id, "args": kwargs}


# --- Pipeline pause / resume ------------------------------------------------------


async def _pipeline_job_running() -> bool:
    """Session wrapper over the shared predicate in `worker.control` — the
    announcement applier asks the same question and must get the same answer."""
    from ..worker.control import pipeline_job_running

    async with SessionLocal() as session:
        return await pipeline_job_running(session)


@router.post("/pipeline/pause")
async def api_pipeline_pause():
    """Pause LLM pipeline work by hand. A running
    worker stops at the next unit boundary — one story/post/batch — and unloads
    the decode models itself; if nothing is running, models are unloaded now.
    The flag persists until /pipeline/resume, so scheduled runs stay no-ops."""
    from ..worker.control import set_paused, unload_unless_interactive

    async with SessionLocal() as session:
        await set_paused(session, True)
    running = await _pipeline_job_running()
    # The unload here is a side effect of pausing the PIPELINE, not a request to
    # take the models away, so an interactive turn survives it. `/llm/unload` is
    # the lever for actually insisting, and it 409s instead of skipping quietly.
    unloaded: list[str] = []
    if not running:
        async with SessionLocal() as session:
            unloaded = await unload_unless_interactive(session)
    return {"paused": True, "worker_running": running, "unloaded_models": unloaded}


@router.post("/pipeline/resume")
async def api_pipeline_resume(run: bool = Query(True)):
    """Clear the pause flag; by default also defer a pipeline run to pick up
    where the pause left off (run=false to just clear the flag). Models reload
    lazily on first use."""
    from ..worker.control import set_paused

    async with SessionLocal() as session:
        await set_paused(session, False)
    job_id = None
    if run:
        from ..worker.app import app as job_app

        async with job_app.open_async():
            job_id = await job_app.configure_task("episteme.run_pipeline").defer_async()
    return {"paused": False, "job_id": job_id}


@router.post("/pipeline/announce")
async def api_pipeline_announce(announcement: Annotated[dict, Body(...)]):
    """llama-warden's verdict, pushed here on every transition (0057).

    `{"action": "pause"|"resume", "reason": str, "since": str, "warden": str}`.
    Only `action` is required — the rest is the warden explaining itself, and a
    consumer that refused the message over a missing field would be paused, or
    running, for the wrong reason.

    Open, like every other route here: single-user, tailnet only, no auth. The
    warden reaches it over loopback from the host.

    **Idempotent**, which is a contract and not an implementation detail: the
    warden re-sends its verdict every 300 s until it changes, so applying `pause`
    to an already-paused pipeline must not re-stamp when the pause began. See
    `worker/contention.py` for what each branch does and why."""
    from ..worker.contention import apply_announcement

    action = (announcement.get("action") or "").strip().lower()
    reason = announcement.get("reason") or "no reason given"
    try:
        async with SessionLocal() as session:
            return await apply_announcement(session, action, reason)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/llm/unload")
async def api_llm_unload(force: bool = Query(False)):
    """Unload all decode models immediately (frees VRAM). Standalone lever —
    does NOT pause the pipeline; in-flight LLM calls will fail and a running
    worker will reload models on its next call. Pair with /pipeline/pause.

    409s while an interactive chat turn holds the models, because unloading
    under one kills a stream somebody is watching. `?force=true` is the escape
    hatch, the same shape /llm/backend/stop already uses: we did not do it, and
    here is how to insist."""
    from ..worker.control import interactive_held

    if not force:
        async with SessionLocal() as session:
            if await interactive_held(session):
                raise HTTPException(
                    409, "An interactive chat turn holds the models; retry with ?force=true"
                )
    return {"unloaded_models": await gateway.unload_models()}


# --- llama.cpp backend lifecycle (via the host control agent) ----------------------
#
# Every route here is a thin pass-through to llama-warden's own service, except the
# stop, which is composed from parts that already exist. All of them 503 rather
# than 500 when the agent is absent: an optional component being absent is a
# service state, not a server error.


async def _agent_call(coro):
    """Turn WardenError into a 503. One helper so every lifecycle route
    reports an unreachable agent the same way."""
    try:
        return await coro
    except WardenError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/llm/backend")
async def api_llm_backend():
    """Process-level view of the backend: which servers are listening, their PIDs
    and uptime. Complements /api/status's endpoint probes, which answer the
    different question of whether the HTTP API responds."""
    return {
        "enabled": warden.enabled,
        "url": settings.llm_warden_url,
        "status": await warden.status(),
    }


@router.get("/llm/resources")
async def api_llm_resources():
    """GPU contention measurements from the host. Its own route because it costs
    ~3.5s (the per-process GPU counter has an irreducible sampling floor), so it
    must never sit on the critical path of a page load."""
    resources = await warden.resources()
    if resources is None:
        raise HTTPException(status_code=503, detail="llama-warden unavailable")
    return resources


@router.get("/llm/logs")
async def api_llm_logs(
    which: str = Query("router"),
    tail: int | None = Query(None),
    since: int | None = Query(None, ge=0),
):
    """A snapshot. `since` (a previous `next_offset`) asks for only what was
    written after it — the same delta the stream below is built on, for scripts
    that would rather poll than hold a connection open."""
    logs = await warden.logs(which, tail, since)
    if logs is None:
        raise HTTPException(status_code=503, detail="llama-warden unavailable")
    return logs


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@router.get("/llm/logs/stream")
async def api_llm_logs_stream(
    request: Request,
    which: str = Query("router"),
    since: int | None = Query(None, ge=0),
):
    """The log pane's transport: server-sent events carrying only the lines
    written since the last offset.

    It replaces a 3s htmx poll that re-fetched and re-swapped the entire 300-line
    tail. Two things were wrong with that beyond the bytes: swapping the pane
    destroyed any text selection the operator had made in it, and reset their
    scrollback — precisely while they were reading the thing they came for.

    **The browser is pushed to; only the container→host leg polls.** A `tail -f`
    held open across the Docker boundary would put a long-lived connection on
    the optional, restartable warden and give it a tailing loop in a
    threadpool worker, to save a delta read that is a file seek. The offset
    protocol makes that leg stateless and idempotent instead, and it is the same
    one `GET /llm/logs?since=` exposes.

    Pass `since` to continue an already-rendered tail without re-sending it: the
    admin fragment renders a snapshot server-side and hands us its `next_offset`,
    so the pane never flashes. Events:

    * `reset` — replace the pane's contents (first payload, a restart-truncated
      log, or a backlog past the window; `gap_bytes` says what was skipped)
    * `lines` — append these
    * `unavailable` — the agent is unreachable; the stream keeps trying, because
      an agent restart is a normal thing to be watching the log for

    Failure is an event rather than a closed connection for that last reason:
    the stream must outlive the process it reports on. It is NOT named `error`,
    which SSE dispatches onto the same handler as EventSource's own transport
    error — one of which carries `data` and one of which does not.
    """
    if not warden.enabled:
        raise HTTPException(status_code=503, detail="llama-warden unavailable")

    interval = settings.llm_log_stream_interval_seconds

    async def events():
        offset = since
        # First payload replaces whatever the pane holds unless the caller told
        # us where its snapshot ended.
        replace = since is None
        silent = 0.0
        while not await request.is_disconnected():
            payload = await warden.logs(which, since=offset)
            if payload is None:
                yield _sse(
                    "unavailable",
                    {"detail": f"Control agent unreachable at {settings.llm_warden_url}"},
                )
                silent = 0.0
            else:
                offset = payload.get("next_offset", offset)
                # No `next_offset` at all means an agent from before the offset
                # protocol — it runs on the host and is deployed separately from
                # this container, so that skew is a normal state. Every answer is
                # then a full tail, and treating it as a replace degrades exactly
                # to the poll this stream came from instead of appending the same
                # 300 lines once a second.
                reset = replace or payload.get("reset") or "next_offset" not in payload
                if reset or payload.get("lines"):
                    yield _sse(
                        "reset" if reset else "lines",
                        {
                            "lines": payload.get("lines", []),
                            "size_bytes": payload.get("size_bytes", 0),
                            "gap_bytes": payload.get("gap_bytes", 0),
                            "exists": payload.get("exists", False),
                            "path": payload.get("path"),
                        },
                    )
                    silent = 0.0
                replace = False
            # A quiet log is the normal state (llama-server logs on request), so
            # the connection has to prove it is alive on its own.
            silent += interval
            if silent >= 15:
                yield ": keep-alive\n\n"
                silent = 0.0
            await asyncio.sleep(interval)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # no-store because a cached event stream is a lie about liveness;
        # X-Accel-Buffering for any reverse proxy that would otherwise buffer.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/llm/backend/start")
async def api_llm_backend_start():
    """Start llama.cpp. Idempotent at the agent — two rapid clicks cannot produce
    two routers."""
    return await _agent_call(warden.start())


@router.post("/llm/backend/restart")
async def api_llm_backend_restart():
    return await _agent_call(warden.restart())


@router.post("/llm/backend/stop")
async def api_llm_backend_stop(force: bool = Query(False)):
    """Stop llama.cpp **without losing work**.

    Composed rather than new: pause the pipeline (the existing flag), wait for
    the worker to finish its current unit — one story/post/batch, which is what
    makes a pause non-destructive in the first place — and only then ask the
    agent to kill the processes.

    If the unit outlasts `llm_graceful_stop_seconds` this returns `stopped:
    false` and leaves everything running, including the pause. Nothing is ever
    killed mid-generation without `force=true`, which is a second deliberate act
    by the caller. The pause is intentionally left set on the timeout path: the
    worker is on its way to a boundary and re-clearing it would send it straight
    back into main-model work.

    **The pause is a side effect, so it is only taken once the stop can plausibly
    happen and it is undone if it doesn't.** The agent is optional and often
    absent; pausing first meant an unreachable agent 503'd *after* leaving the
    pipeline paused as MANUAL — which the warden is forbidden to lift — with
    nothing stopped and no llama.cpp problem to explain it. So: pre-flight the
    agent before writing anything, and roll the flag back to exactly what it was
    if the kill itself fails. Only the timeout path deliberately keeps it.
    """
    from ..worker.control import pause_state, restore_pause, set_paused

    if not warden.enabled:
        raise HTTPException(status_code=503, detail="No warden configured")
    if await warden.status() is None:
        raise HTTPException(status_code=503, detail="llama-warden unavailable")

    async with SessionLocal() as session:
        before = await pause_state(session)
        await set_paused(session, True)

    async def _rollback() -> None:
        async with SessionLocal() as session:
            await restore_pause(session, before)

    waited = 0.0
    deadline = settings.llm_graceful_stop_seconds
    while not force and await _pipeline_job_running():
        if waited >= deadline:
            return {
                "stopped": False,
                "paused": True,
                "reason": (
                    f"pipeline still running after {waited:.0f}s; it will stop at the "
                    "next unit boundary. Retry, or pass force=true to kill it now."
                ),
                "waited_seconds": waited,
            }
        await asyncio.sleep(2.0)
        waited += 2.0

    try:
        result = await _agent_call(warden.stop())
    except HTTPException:
        await _rollback()
        raise
    return {"stopped": True, "paused": True, "waited_seconds": waited, **result}


# --- Stories ----------------------------------------------------------------------


@router.get("/stories")
async def api_stories(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    query = select(Story).order_by(Story.id.desc()).offset(offset).limit(limit)
    if status:
        query = query.where(Story.status == status)
    async with SessionLocal() as session:
        stories = (await session.execute(query)).scalars().all()
    return [_story_dict(s) for s in stories]


@router.get("/stories/{story_id}")
async def api_story(story_id: int):
    from sqlalchemy.orm import selectinload

    async with SessionLocal() as session:
        story = (
            await session.execute(
                select(Story)
                .options(selectinload(Story.items).joinedload(SourceItem.source))
                .where(Story.id == story_id)
            )
        ).scalar_one_or_none()
        if story is None:
            raise HTTPException(404)
        posts = (
            (await session.execute(select(Post).where(Post.story_id == story_id))).scalars().all()
        )
    return {
        **_story_dict(story),
        "items": [_item_dict(i) for i in story.items],
        "posts": [_post_dict(p) for p in posts],
    }


@router.get("/stories/{story_id}/llm-calls")
async def api_story_llm_calls(story_id: int, full: bool = False):
    async with SessionLocal() as session:
        calls = (
            (
                await session.execute(
                    select(LlmCall).where(LlmCall.story_id == story_id).order_by(LlmCall.id)
                )
            )
            .scalars()
            .all()
        )
    return [_llm_call_dict(c, full=full) for c in calls]


@router.get("/posts/{post_id}/llm-calls")
async def api_post_llm_calls(post_id: int, full: bool = False):
    """Provenance of ONE post generation: the calls stamped with this post_id
    plus the story-level calls every version shares. Unstamped write-side rows
    (an attempt that never produced a post) stay out —
    /api/stories/{id}/llm-calls remains the unfiltered story history."""
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None:
            raise HTTPException(404)
        calls = (
            (
                await session.execute(
                    select(LlmCall)
                    .where(
                        or_(
                            LlmCall.post_id == post_id,
                            and_(
                                LlmCall.story_id == post.story_id,
                                LlmCall.post_id.is_(None),
                                LlmCall.stage.notin_(POST_SCOPED_STAGES),
                            ),
                        )
                    )
                    .order_by(LlmCall.id)
                )
            )
            .scalars()
            .all()
        )
    return [_llm_call_dict(c, full=full) for c in calls]


# --- Posts ------------------------------------------------------------------------


@router.get("/posts")
async def api_posts(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    async with SessionLocal() as session:
        posts = (
            (
                await session.execute(
                    select(Post).order_by(Post.id.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [_post_dict(p) for p in posts]


# Declared above `/posts/{post_id}`: routes match in declaration order, and
# `post_id: int` would reject "search" as a 422 rather than fall through to here.
@router.get("/posts/search")
async def api_posts_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
):
    """Find published article posts: literal title/summary match merged above
    semantic neighbours (recommend.search). Exposed as its own endpoint so the
    ranking is scriptable and checkable without going through the assistant."""
    async with SessionLocal() as session:
        posts = await search_posts(session, q, limit=limit)
    return [_post_dict(p) for p in posts]


@router.post("/posts/{post_id}/pin")
async def api_post_pin(post_id: int, value: Annotated[bool, Query()] = True):
    """Retention override: a pinned post is never auto-pruned, and its
    provenance page's calls (stamped + shared story-level) are kept with it —
    e.g. keep a superseded draft around for a later side-by-side comparison.
    ?value=false unpins."""
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None:
            raise HTTPException(404)
        post.pinned = value
        await session.commit()
        return {"id": post.id, "pinned": post.pinned}


@router.get("/posts/{post_id}/audio")
async def api_post_audio(post_id: int, voice: str | None = None):
    """Narration status for a post in one voice (defaults to the catalog default).
    Returns {status: none|pending|ready|failed, url, voice}; `url` is set only when
    ready. The web voice selector uses this as a lightweight cache probe."""
    async with SessionLocal() as session:
        voice = await default_voice_id(session, voice)
        row = (
            await session.execute(
                select(PostAudio).where(PostAudio.post_id == post_id, PostAudio.voice == voice)
            )
        ).scalar_one_or_none()
    if row is None:
        return {"post_id": post_id, "voice": voice, "status": "none", "url": None}
    return {
        "post_id": post_id,
        "voice": voice,
        "status": row.status,
        "url": f"/media/{row.path}" if row.status == "ready" and row.path else None,
        "error": row.error,
    }


def _audio_is_current(row: PostAudio | None, digest: str, fmt: str, params: dict) -> bool:
    """A cached row is a hit only if it's ready for THIS script, format, and voice
    params — otherwise a QA revision or a params change silently serves stale audio."""
    return bool(
        row
        and row.status == "ready"
        and row.path
        and row.script_hash == digest
        and row.audio_format == fmt
        and (row.params or {}) == (params or {})
    )


@router.get("/posts/{post_id}/audio/stream")
async def api_post_audio_stream(post_id: int, request: Request, voice: str | None = None):
    """Primary playback URL: stream the post's narration in one voice. A current
    cache file is served with Range support (seekable); otherwise it is
    synthesized live (opus over Fish's WebSocket), streamed to the client AND
    tee'd to the cache — so playback starts immediately and a repeat play, or a
    disconnect mid-stream, still ends with a stored, reusable file."""
    fmt = settings.tts_audio_format
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None or post.kind != "article":
            raise HTTPException(404)
        voice_id = voice or await default_voice_id(session, settings.tts_default_voice)
        voice_obj = await get_voice(session, voice_id)
        if voice_obj is None:
            raise HTTPException(400, f"Unknown voice {voice_id!r}")
        script = build_script(post)
        if not script.strip():
            raise HTTPException(404, "nothing narratable in this post")
        digest = script_hash(script)
        row = (
            await session.execute(
                select(PostAudio).where(PostAudio.post_id == post_id, PostAudio.voice == voice_id)
            )
        ).scalar_one_or_none()

    audio_root = Path(settings.audio_dir)
    media_type = media_type_for(fmt)
    if _audio_is_current(row, digest, fmt, voice_obj.params):
        cached = audio_root / row.path
        if cached.exists():
            # The audio is content-addressable by script hash, so the digest is a
            # strong validator: a replay or a seek revalidates and gets a body-less
            # 304 instead of re-downloading the whole file. `no-cache` (store, always
            # revalidate) keeps a QA revision from ever serving stale audio — though a
            # revised script also changes `digest`, so this branch wouldn't be hit.
            etag = f'"{digest}"'
            headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return FileResponse(cached, media_type=media_type, headers=headers)

    if not settings.fish_api_key:
        raise HTTPException(503, "narration is not configured")

    filename = f"{post_id}-{voice_id}.{fmt}"
    common = dict(
        provider=voice_obj.provider,
        params=voice_obj.params,
        audio_format=fmt,
        char_count=len(script),
        script_hash=digest,
    )
    async with SessionLocal() as session:
        await upsert_post_audio(session, post_id, voice_id, status="pending", error=None, **common)
        await session.commit()

    async def on_success(result) -> None:
        async with SessionLocal() as session:
            await upsert_post_audio(
                session,
                post_id,
                voice_id,
                status="ready",
                path=result.path.name,
                model=settings.tts_model,
                error=None,
                **common,
            )
            await session.commit()

    async def on_failure(exc: BaseException) -> None:
        async with SessionLocal() as session:
            await upsert_post_audio(
                session, post_id, voice_id, status="failed", error=str(exc), **common
            )
            await session.commit()

    body = await tee_to_client(
        text=script,
        dest=audio_root / filename,
        on_success=on_success,
        on_failure=on_failure,
        api_key=settings.fish_api_key,
        model=settings.tts_model,
        ref_id=voice_obj.ref_id,
        params=voice_obj.params,
        audio_format=fmt,
        opus_bitrate=settings.tts_opus_bitrate,
        mp3_bitrate=settings.tts_mp3_bitrate,
        latency=settings.tts_latency,
        base_url=settings.tts_fish_base_url,
    )
    return StreamingResponse(body, media_type=media_type)


@router.get("/posts/{post_id}/next")
async def api_post_next(post_id: int):
    """The next published ARTICLE post after this one in feed order (articles sort
    by generated_at DESC; see web.app._feed_page) — drives continuous ("podcast")
    playback. Returns {next_id, url} or nulls at the end of the feed."""
    async with SessionLocal() as session:
        current = await session.get(Post, post_id)
        if current is None:
            raise HTTPException(404)
        nxt = (
            await session.execute(
                select(Post.id)
                .where(
                    Post.kind == "article",
                    Post.status == "published",
                    or_(
                        Post.generated_at < current.generated_at,
                        and_(
                            Post.generated_at == current.generated_at,
                            Post.id < current.id,
                        ),
                    ),
                )
                .order_by(Post.generated_at.desc(), Post.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return {
        "next_id": nxt,
        "url": f"/post/{nxt}" if nxt else None,
    }


@router.post("/posts/{post_id}/narrate")
async def api_post_narrate(post_id: int, voice: str | None = None):
    """Explicitly (pre-)generate narration for an article post via the worker
    stage — the batch path (the post page streams on demand instead). Defers a
    narrate job; the caller polls GET /api/posts/{id}/audio until `ready`.
    Idempotent: the stage skips a voice already synthesized from the current
    script + params."""
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None:
            raise HTTPException(404)
        if post.kind != "article":
            raise HTTPException(400, "only article posts can be narrated")
        voice = voice or await default_voice_id(session, settings.tts_default_voice)
        if await get_voice(session, voice) is None:
            raise HTTPException(400, f"Unknown voice {voice!r}")
    from ..worker.app import app as job_app

    async with job_app.open_async():
        job_id = await job_app.configure_task("episteme.pipeline_stage").defer_async(
            stage="narrate", post_id=post_id, voice=voice
        )
    return {"deferred": "narrate", "job_id": job_id, "post_id": post_id, "voice": voice}


@router.get("/posts/{post_id}")
async def api_post(post_id: int):
    async with SessionLocal() as session:
        post = (await session.execute(select(Post).where(Post.id == post_id))).scalar_one_or_none()
    if post is None:
        raise HTTPException(404)
    return _post_dict(post, with_sections=True)


# --- Feedback + interest profile --------------------------------------------------


def _feedback_dict(event: Feedback) -> dict:
    return {
        "id": event.id,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "kind": event.kind,
        "post_id": event.post_id,
        "topic": event.topic,
        "value": event.value,
        "keyword": event.keyword,
        "source_ids": event.source_ids or [],
        "nl_text": event.nl_text,
        "parsed_intent": event.parsed_intent,
    }


def _profile_dict(state) -> dict:
    return {
        "event_count": state.event_count,
        "topic_weights": state.topic_weights,
        "source_weights": state.source_weights,
        "difficulty_weights": state.difficulty_weights,
        "blocked_keywords": state.blocked_keywords,
        "blocked_sources": state.blocked_sources,
        "intent_statement": state.intent_statement,
        "has_liked_centroid": state.liked_centroid is not None,
        "has_disliked_centroid": state.disliked_centroid is not None,
    }


@router.post("/feedback")
async def api_feedback(
    kind: str,
    post_id: int | None = Query(None, ge=1),
    topic: str | None = None,
    source_id: int | None = Query(None, ge=1),
    weight: float | None = None,
    keyword: str | None = None,
):
    """Record one explicit signal. The profile is rebuilt from the whole log
    immediately, so the next feed read already reflects it.

    `weight` belongs to `kind=set_topic` — the hand-edited weight from /tune,
    which is absolute (it replaces the topic's accumulated weight) rather than a
    step. `keyword` belongs to `block_keyword` / `unblock_keyword`. All of them are
    events like any other, so undo works the same way."""
    async with SessionLocal() as session:
        try:
            event, state = await feedback.record(
                session,
                kind,
                post_id=post_id,
                topic=topic,
                source_id=source_id,
                weight=weight,
                keyword=keyword,
            )
        except feedback.FeedbackError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"feedback": _feedback_dict(event), "profile": _profile_dict(state)}


@router.post("/feedback/nl")
async def api_feedback_nl(text: str = Query(..., min_length=1)):
    """Natural-language feedback: parsed into structured intent by the fast model,
    applied, and echoed back so the interpretation is confirmable."""
    async with SessionLocal() as session:
        try:
            event, state, echo = await feedback.record_nl(session, text)
        except feedback.FeedbackError as exc:
            raise HTTPException(422, str(exc)) from None
        return {
            "feedback": _feedback_dict(event),
            "echo": echo,
            "profile": _profile_dict(state),
        }


@router.delete("/feedback/{feedback_id}")
async def api_feedback_undo(feedback_id: int):
    """Undo is exact: the event is deleted and the profile replayed without it."""
    async with SessionLocal() as session:
        try:
            state = await feedback.undo(session, feedback_id)
        except feedback.FeedbackError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"undone": feedback_id, "profile": _profile_dict(state)}


@router.get("/feedback")
async def api_feedback_list(limit: int = Query(50, ge=1, le=500)):
    async with SessionLocal() as session:
        events = await feedback.recent(session, limit=limit)
    return [_feedback_dict(event) for event in events]


@router.get("/profile")
async def api_profile():
    """The derived profile as currently cached. `event_count` and `rebuilt_at`
    are the staleness check — the cache is only ever a replay of /api/feedback."""
    async with SessionLocal() as session:
        state = await profile.load(session)
        row = await session.get(InterestProfile, profile.PROFILE_ID)
    return {
        **_profile_dict(state),
        "rebuilt_at": row.rebuilt_at.isoformat() if row and row.rebuilt_at else None,
    }


@router.post("/profile/rebuild")
async def api_profile_rebuild():
    """Force a replay — after retuning the half-life or signal weights in config,
    which reinterprets the entire history rather than leaving it baked in."""
    async with SessionLocal() as session:
        state = await profile.rebuild(session)
    return _profile_dict(state)


# --- Topic vocabulary -------------------------------------------------------------


@router.get("/topics")
async def api_topics():
    """The canonical vocabulary the interest profile learns weights over, with
    how often each entry is actually used (dead entries are the ones to merge)."""
    async with SessionLocal() as session:
        entries = await topics.vocabulary(session)
        counts = await topics.usage_counts(session)
        proposal = await topics.get_proposal(session)
    known = {topic.label for topic in entries}
    return {
        "topics": sorted(
            (
                {
                    "slug": topic.slug,
                    "label": topic.label,
                    "aliases": topic.aliases or [],
                    "uses": counts.get(topic.label, 0),
                    "embedded": topic.embedding is not None,
                }
                for topic in entries
            ),
            key=lambda entry: (-entry["uses"], entry["label"]),
        ),
        # Labels in use that no vocabulary entry claims — i.e. what the bootstrap
        # would still have to fold in. Empty is the healthy steady state.
        "unresolved": sorted(
            {label: uses for label, uses in counts.items() if label not in known}.items(),
            key=lambda pair: (-pair[1], pair[0]),
        ),
        "has_proposal": proposal is not None,
    }


@router.get("/topics/proposal")
async def api_topics_proposal():
    """The pending vocabulary proposal (phase one of the bootstrap), or 404 if
    none is awaiting review."""
    async with SessionLocal() as session:
        proposal = await topics.get_proposal(session)
    if proposal is None:
        raise HTTPException(404, "No topic vocabulary proposal awaiting review")
    return proposal


@router.delete("/topics/proposal")
async def api_topics_proposal_discard():
    async with SessionLocal() as session:
        await topics.discard_proposal(session)
    return {"discarded": True}


@router.post("/topics/{slug}/rename")
async def api_topic_rename(slug: str, label: Annotated[str, Query(min_length=1)]):
    """Rename a vocabulary entry, rewriting every story/post that references it —
    labels are stored, not ids, so this is the only sanctioned way to do it."""
    async with SessionLocal() as session:
        try:
            rewritten = await topics.rename(session, slug, label)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
    return {"slug": slug, "label": label, "rows_rewritten": rewritten}


@router.post("/topics/{slug}/merge")
async def api_topic_merge(slug: str, into: Annotated[str, Query(min_length=1)]):
    """Fold one vocabulary entry into another (aliases move, rows are rewritten,
    the absorbed entry is deleted).

    A merge ends one of the two identities, so it repoints the feedback log at the
    survivor and the derived profile has to be replayed to see it — and every
    stored `affinity_score` was computed against the pre-merge weights. Rename
    needs neither: the slug it is keyed by never moves."""
    async with SessionLocal() as session:
        try:
            rewritten = await topics.merge(session, slug, into)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        await profile.rebuild(session)
    await defer_rescore()
    return {"slug": slug, "merged_into": into, "rows_rewritten": rewritten}


# --- LLM calls --------------------------------------------------------------------


@router.get("/llm-calls")
async def api_llm_calls(
    story_id: int | None = None,
    stage: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    full: bool = False,
):
    query = select(LlmCall).order_by(LlmCall.id.desc()).limit(limit)
    if story_id is not None:
        query = query.where(LlmCall.story_id == story_id)
    if stage:
        query = query.where(LlmCall.stage == stage)
    async with SessionLocal() as session:
        calls = (await session.execute(query)).scalars().all()
    return [_llm_call_dict(c, full=full) for c in calls]


@router.post("/llm-calls/pin")
async def api_llm_calls_pin(attempt_id: str, value: bool = Query(True)):
    """Retention override for calls with no post row to pin through: a failed
    generation attempt's write-side calls (post_id NULL), addressed by the
    attempt_id shown in /api/stories/{id}/llm-calls. ?value=false unpins."""
    async with SessionLocal() as session:
        result = await session.execute(
            update(LlmCall).where(LlmCall.attempt_id == attempt_id).values(pinned=value)
        )
        await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, f"no llm_calls with attempt_id {attempt_id!r}")
    return {"attempt_id": attempt_id, "pinned": value, "calls": result.rowcount}
