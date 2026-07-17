"""JSON API under /api — the scriptable face of the app.

Single-user, no auth (decided constraint). The admin UI renders the same data
as HTML; scripts and future integrations use these endpoints. Serializers are
plain dict builders — the ORM models are the schema."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text

from ..config import settings
from ..db import SessionLocal
from ..llm import gateway
from ..models import Article, LlmCall, PipelineRun, Source, SourceItem, Story

router = APIRouter(prefix="/api")

# Tasks the defer endpoint may enqueue — everything else is 404.
DEFERRABLE_TASKS = {
    "ingest_all": "episteme.ingest_all",
    "run_pipeline": "episteme.run_pipeline",
}


# --- Serializers ------------------------------------------------------------------


def _source_dict(source: Source) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "type": source.type_name,
        "enabled": source.enabled,
        "http_mode": (source.config or {}).get("http_mode", "polite"),
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


def _article_dict(article: Article, with_sections: bool = False) -> dict:
    data = {
        "id": article.id,
        "story_id": article.story_id,
        "title": article.title,
        "summary": article.summary,
        "difficulty": article.difficulty,
        "topics": article.topics,
        "reading_time_minutes": article.reading_time_minutes,
        "model_used": article.model_used,
        "status": article.status,
        "generated_at": article.generated_at,
    }
    if with_sections:
        data["sections"] = article.sections
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
    async with SessionLocal() as session:
        story_counts = dict(
            (await session.execute(select(Story.status, func.count()).group_by(Story.status))).all()
        )
        item_total = (await session.execute(select(func.count()).select_from(SourceItem))).scalar_one()
        unembedded = (
            await session.execute(
                select(func.count()).select_from(SourceItem).where(SourceItem.embedding.is_(None))
            )
        ).scalar_one()
        article_total = (await session.execute(select(func.count()).select_from(Article))).scalar_one()
        last_run = (
            await session.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1))
        ).scalar_one_or_none()

    llm_models: list[dict] | None = None
    llm_available = await gateway.is_available()
    if llm_available:
        try:
            llm_models = [
                {"id": m.get("id"), "status": (m.get("status") or {}).get("value")}
                for m in await gateway.list_models()
            ]
        except Exception:
            llm_models = None

    return {
        "llm": {
            "base_url": settings.llm_base_url,
            "available": llm_available,
            "models": llm_models,
            "roles": {
                "writer": settings.llm_model_writer,
                "fast": settings.llm_model_fast,
                "embed": settings.llm_model_embed,
            },
        },
        "stories": story_counts,
        "source_items": {"total": item_total, "unembedded": unembedded},
        "articles": article_total,
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


# --- Pipeline runs ----------------------------------------------------------------


@router.get("/runs")
async def api_runs(limit: int = Query(20, ge=1, le=200)):
    async with SessionLocal() as session:
        runs = (
            (await session.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)))
            .scalars()
            .all()
        )
    return [_run_dict(r) for r in runs]


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


@router.post("/jobs/defer/{task}")
async def api_defer(task: str):
    task_name = DEFERRABLE_TASKS.get(task)
    if task_name is None:
        raise HTTPException(404, f"Unknown task {task!r}; deferrable: {sorted(DEFERRABLE_TASKS)}")
    from ..worker.app import app as job_app

    async with job_app.open_async():
        job = await job_app.configure_task(task_name).defer_async()
    return {"deferred": task_name, "job_id": job.id}


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
        articles = (
            (await session.execute(select(Article).where(Article.story_id == story_id)))
            .scalars()
            .all()
        )
    return {
        **_story_dict(story),
        "items": [_item_dict(i) for i in story.items],
        "articles": [_article_dict(a) for a in articles],
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


# --- Articles ---------------------------------------------------------------------


@router.get("/articles")
async def api_articles(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    async with SessionLocal() as session:
        articles = (
            (
                await session.execute(
                    select(Article).order_by(Article.id.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [_article_dict(a) for a in articles]


@router.get("/articles/{article_id}")
async def api_article(article_id: int):
    async with SessionLocal() as session:
        article = (
            await session.execute(select(Article).where(Article.id == article_id))
        ).scalar_one_or_none()
    if article is None:
        raise HTTPException(404)
    return _article_dict(article, with_sections=True)


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
