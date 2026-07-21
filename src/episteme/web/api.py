"""JSON API under /api — the scriptable face of the app.

Single-user, no auth (decided constraint). The admin UI renders the same data
as HTML; scripts and future integrations use these endpoints. Serializers are
plain dict builders — the ORM models are the schema."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from croniter import croniter
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import and_, func, or_, select, text, update

from ..config import settings
from ..db import SessionLocal
from ..llm import gateway
from ..models import (
    POST_SCOPED_STAGES,
    LlmCall,
    PipelineRun,
    Post,
    PostAudio,
    Source,
    SourceItem,
    Story,
)
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
    "embed": ("episteme.pipeline_stage", frozenset({"limit"})),
    "cluster": ("episteme.pipeline_stage", frozenset({"limit"})),
    "triage": ("episteme.pipeline_stage", frozenset({"limit", "story_id"})),
    "write": ("episteme.pipeline_stage", frozenset({"limit", "story_id"})),
    "qa": ("episteme.pipeline_stage", frozenset({"limit", "post_id"})),
    "narrate": ("episteme.pipeline_stage", frozenset({"limit", "post_id"})),
}


def defer_args(task: str, **params: int | None) -> tuple[str, dict]:
    """Validate a defer request; returns (procrastinate task name, job kwargs).
    Raises KeyError for unknown tasks, ValueError for bad params — the endpoint
    maps those to 404/422. Pure so it's unit-testable without a queue."""
    try:
        task_name, allowed = DEFERRABLE_TASKS[task]
    except KeyError:
        raise KeyError(
            f"Unknown task {task!r}; deferrable: {sorted(DEFERRABLE_TASKS)}"
        ) from None
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


@router.get("/status")
async def api_status():
    from ..worker.control import pause_requested

    async with SessionLocal() as session:
        paused = await pause_requested(session)
        story_counts = dict(
            (await session.execute(select(Story.status, func.count()).group_by(Story.status))).all()
        )
        item_total = (await session.execute(select(func.count()).select_from(SourceItem))).scalar_one()
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

    llm_models: list[dict] | None = None
    try:
        llm_models = [
            {"id": m.get("id"), "status": (m.get("status") or {}).get("value")}
            for m in await gateway.list_models()
        ]
    except Exception:
        llm_models = None

    endpoints = await gateway.endpoint_status()

    return {
        "llm": {
            "base_url": settings.llm_base_url,
            "embed_base_url": settings.llm_embed_base_url,
            "available": all(e["available"] for e in endpoints),
            "endpoints": endpoints,
            "models": llm_models,
            "roles": {
                "main": settings.llm_model_main,
                "fast": settings.llm_model_fast,
                "embed": settings.llm_model_embed,
            },
        },
        "pipeline": {"paused": paused},
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
    active_cooldowns = [
        s for s in sources if s["cooldown_until"] and s["cooldown_until"] > now
    ]
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
async def api_runs(limit: int = Query(20, ge=1, le=200)):
    async with SessionLocal() as session:
        runs = (
            (await session.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)))
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


@router.get("/jobs")
async def api_jobs(status: str | None = None, limit: int = Query(50, ge=1, le=500)):
    query = (
        "SELECT id, task_name, status, args, attempts, scheduled_at "
        "FROM procrastinate_jobs "
        + ("WHERE status = :status " if status else "")
        + "ORDER BY id DESC LIMIT :limit"
    )
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    async with SessionLocal() as session:
        rows = (await session.execute(text(query), params)).mappings().all()
    return [dict(row) for row in rows]


_JOB_COLUMNS = "id, task_name, status, args, attempts, scheduled_at"


@router.get("/jobs/summary")
async def api_jobs_summary():
    """Condensed queue state for the dashboard: what's running now, what's next
    in line (earliest scheduled todo), and the most recently finished job."""
    async with SessionLocal() as session:
        running = (
            await session.execute(
                text(
                    f"SELECT {_JOB_COLUMNS} FROM procrastinate_jobs "
                    "WHERE status = 'doing' ORDER BY id DESC LIMIT 5"
                )
            )
        ).mappings().all()
        upcoming = (
            await session.execute(
                text(
                    f"SELECT {_JOB_COLUMNS} FROM procrastinate_jobs WHERE status = 'todo' "
                    "ORDER BY scheduled_at ASC NULLS FIRST, id ASC LIMIT 1"
                )
            )
        ).mappings().first()
        recent = (
            await session.execute(
                text(
                    f"SELECT {_JOB_COLUMNS} FROM procrastinate_jobs "
                    "WHERE status NOT IN ('todo', 'doing') ORDER BY id DESC LIMIT 1"
                )
            )
        ).mappings().first()
    return {
        "running": [dict(r) for r in running],
        "upcoming": dict(upcoming) if upcoming else None,
        "recent": dict(recent) if recent else None,
    }


@router.post("/jobs/defer/{task}")
async def api_defer(
    task: str,
    limit: int | None = Query(None, ge=1),
    story_id: int | None = Query(None, ge=1),
    post_id: int | None = Query(None, ge=1),
    source_id: int | None = Query(None, ge=1),
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
    from ..worker.app import app as job_app

    async with job_app.open_async():
        job_id = await job_app.configure_task(task_name).defer_async(**kwargs)
    return {"deferred": task_name, "job_id": job_id, "args": kwargs}


# --- Pipeline pause / resume ------------------------------------------------------


async def _pipeline_job_running() -> bool:
    query = text(
        "SELECT count(*) FROM procrastinate_jobs WHERE status = 'doing' "
        "AND task_name IN ('episteme.run_pipeline', 'episteme.pipeline_stage')"
    )
    async with SessionLocal() as session:
        return bool((await session.execute(query)).scalar())


@router.post("/pipeline/pause")
async def api_pipeline_pause():
    """Pause LLM pipeline work (for a resource governor or manually). A running
    worker stops at the next unit boundary — one story/post/batch — and unloads
    the decode models itself; if nothing is running, models are unloaded now.
    The flag persists until /pipeline/resume, so scheduled runs stay no-ops."""
    from ..worker.control import set_paused

    async with SessionLocal() as session:
        await set_paused(session, True)
    running = await _pipeline_job_running()
    unloaded = [] if running else await gateway.unload_models()
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


@router.post("/llm/unload")
async def api_llm_unload():
    """Unload all decode models immediately (frees VRAM). Standalone lever —
    does NOT pause the pipeline; in-flight LLM calls will fail and a running
    worker will reload models on its next call. Pair with /pipeline/pause."""
    return {"unloaded_models": await gateway.unload_models()}


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
            (await session.execute(select(Post).where(Post.story_id == story_id)))
            .scalars()
            .all()
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


@router.post("/posts/{post_id}/pin")
async def api_post_pin(post_id: int, value: bool = Query(True)):
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
                select(PostAudio).where(
                    PostAudio.post_id == post_id, PostAudio.voice == voice
                )
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
async def api_post_audio_stream(post_id: int, voice: str | None = None):
    """Primary playback URL: stream the post's narration in one voice. A current
    cache file is served with Range support (seekable); otherwise it is
    synthesized live (opus over Fish's WebSocket), streamed to the client AND
    tee'd to the cache — so playback starts immediately and a repeat play, or a
    disconnect mid-stream, still ends with a stored, reusable file."""
    fmt = settings.tts_audio_format
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None or post.kind != "feature":
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
                select(PostAudio).where(
                    PostAudio.post_id == post_id, PostAudio.voice == voice_id
                )
            )
        ).scalar_one_or_none()

    audio_root = Path(settings.audio_dir)
    media_type = media_type_for(fmt)
    if _audio_is_current(row, digest, fmt, voice_obj.params):
        cached = audio_root / row.path
        if cached.exists():
            return FileResponse(cached, media_type=media_type)

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
                session, post_id, voice_id, status="ready",
                path=result.path.name, model=settings.tts_model, error=None, **common,
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
    """The next published FEATURE post after this one in feed order (features sort
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
                    Post.kind == "feature",
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
    """Explicitly (pre-)generate narration for a feature post via the worker
    stage — the batch path (the post page streams on demand instead). Defers a
    narrate job; the caller polls GET /api/posts/{id}/audio until `ready`.
    Idempotent: the stage skips a voice already synthesized from the current
    script + params."""
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None:
            raise HTTPException(404)
        if post.kind != "feature":
            raise HTTPException(400, "only feature posts can be narrated")
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
        post = (
            await session.execute(select(Post).where(Post.id == post_id))
        ).scalar_one_or_none()
    if post is None:
        raise HTTPException(404)
    return _post_dict(post, with_sections=True)


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
