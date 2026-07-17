"""LLM processing pipeline (spec §7): embed -> cluster -> triage -> write.

Stages are plain async functions wrapped in procrastinate tasks, so the nightly
orchestrator calls them in role-batched order (all fast-model work, then all
main-model work — one model load each), while individual stages stay manually
deferrable for testing.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..llm import LLMError, gateway
from ..llm.agent import run_writer_loop
from ..llm.observe import llm_context
from ..llm.prompts import SUMMARIZE_SYSTEM, TRIAGE_SYSTEM, WRITER_AGENT_SYSTEM
from ..llm.schemas import SourceSummary, TriageResult
from ..models import LlmCall, PipelineRun, Post, SourceItem, Story
from .app import app

log = logging.getLogger("episteme.pipeline")

_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(item: SourceItem) -> str:
    if item.extracted_text:
        return item.extracted_text
    if item.raw_content:
        return _TAG_RE.sub(" ", item.raw_content)
    return ""


def _item_time(item: SourceItem) -> datetime:
    return item.published_at or item.created_at


def update_centroid(centroid: list[float], count: int, embedding: list[float]) -> list[float]:
    """Incremental mean of unit vectors, re-normalized (pure; unit-tested)."""
    merged = [(c * count + e) / (count + 1) for c, e in zip(centroid, embedding, strict=True)]
    norm = sum(x * x for x in merged) ** 0.5 or 1.0
    return [x / norm for x in merged]


# --- Stage 1: embed -------------------------------------------------------------


async def embed_new_items(session: AsyncSession) -> int:
    items = (
        (
            await session.execute(
                select(SourceItem)
                .where(SourceItem.embedding.is_(None))
                .order_by(SourceItem.id)
            )
        )
        .scalars()
        .all()
    )
    embedded = 0
    for start in range(0, len(items), settings.embed_batch_size):
        batch = items[start : start + settings.embed_batch_size]
        texts = [f"{item.title or ''}\n{_plain_text(item)[:1500]}" for item in batch]
        with llm_context(stage="embed"):
            vectors = await gateway.embed(texts)
        for item, vector in zip(batch, vectors, strict=True):
            item.embedding = vector
        await session.commit()
        embedded += len(batch)
    return embedded


# --- Stage 2: cluster ------------------------------------------------------------


async def cluster_items(session: AsyncSession) -> int:
    items = (
        (
            await session.execute(
                select(SourceItem)
                .where(SourceItem.embedding.is_not(None), SourceItem.story_id.is_(None))
                .order_by(SourceItem.published_at.asc().nulls_last(), SourceItem.id)
            )
        )
        .scalars()
        .all()
    )
    max_distance = 1.0 - settings.cluster_similarity_threshold
    clustered = 0
    for item in items:
        cutoff = _item_time(item) - timedelta(days=settings.cluster_window_days)
        distance_col = Story.centroid.cosine_distance(item.embedding).label("distance")
        nearest = (
            await session.execute(
                select(Story, distance_col)
                .where(Story.last_item_at >= cutoff, Story.centroid.is_not(None))
                .order_by(distance_col)
                .limit(1)
            )
        ).first()

        if nearest is not None and nearest.distance <= max_distance:
            story = nearest.Story
            story.centroid = update_centroid(
                list(story.centroid), story.item_count, list(item.embedding)
            )
            story.item_count += 1
            story.last_item_at = max(story.last_item_at, _item_time(item))
            story.first_item_at = min(story.first_item_at, _item_time(item))
        else:
            story = Story(
                centroid=list(item.embedding),
                item_count=1,
                first_item_at=_item_time(item),
                last_item_at=_item_time(item),
            )
            session.add(story)
            await session.flush()
        item.story_id = story.id
        clustered += 1
    await session.commit()
    return clustered


# --- Stage 3: triage -------------------------------------------------------------


def _story_digest(items: list[SourceItem], snippet_chars: int = 300) -> str:
    lines = []
    for item in items[:6]:
        snippet = _plain_text(item)[:snippet_chars].strip()
        lines.append(f"- [{item.source.name}] {item.title}\n  {snippet}")
    return "\n".join(lines)


async def triage_stories(session: AsyncSession) -> int:
    stories = (
        (
            await session.execute(
                select(Story).where(Story.status == "new", Story.item_count > 0)
            )
        )
        .scalars()
        .all()
    )
    triaged = 0
    for story in stories:
        items = await _story_items(session, story)
        digest = _story_digest(items)
        try:
            with llm_context(stage="triage", story_id=story.id):
                result = await gateway.complete_json(
                    "fast", TRIAGE_SYSTEM, f"Story items:\n\n{digest}", TriageResult
                )
        except LLMError as exc:
            log.warning("Triage failed for story %d: %s", story.id, exc)
            continue
        story.triage_decision = result.decision
        story.triage_reason = result.reason
        story.topics = result.topics
        story.rank_score = result.quality_score
        story.status = {
            "write": "triaged",
            "aggregate": "aggregated",
            "skip": "skipped",
        }[result.decision]
        await session.commit()
        triaged += 1
    return triaged


# --- Stage 4: write --------------------------------------------------------------


async def _story_items(session: AsyncSession, story: Story) -> list[SourceItem]:
    from sqlalchemy.orm import joinedload

    return list(
        (
            await session.execute(
                select(SourceItem)
                .options(joinedload(SourceItem.source))
                .where(SourceItem.story_id == story.id)
                .order_by(SourceItem.published_at.asc().nulls_last())
                .limit(settings.max_sources_per_story)
            )
        )
        .scalars()
        .all()
    )


async def _condensed_source(item: SourceItem) -> str:
    text = _plain_text(item)
    if len(text) > settings.summarize_above_chars:
        with llm_context(stage="condense"):
            summary = await gateway.complete_json(
                "fast",
                SUMMARIZE_SYSTEM,
                f"Title: {item.title}\n\n{text[: settings.summarize_above_chars * 4]}",
                SourceSummary,
            )
        return summary.summary
    return text[: settings.summarize_above_chars]


def _sources_section(items: list[SourceItem]) -> dict:
    """Built from the database, never by the LLM — citations cannot hallucinate."""
    return {
        "type": "sources",
        "items": [
            {"title": item.title or item.url, "url": item.url, "outlet": item.source.name}
            for item in items
        ],
    }


def _further_reading_section(fetch_log: list[dict], item_urls: set[str]) -> dict | None:
    """Built by code from the research agent's fetch log — the URLs it actually
    retrieved, never model free-text — so enrichment links can't be hallucinated
    either. Excludes URLs already in the DB-built sources section."""
    from urllib.parse import urlparse

    seen: set[str] = set()
    items = []
    for entry in fetch_log:
        url = entry.get("url", "")
        if not url or url in item_urls or url in seen:
            continue
        seen.add(url)
        items.append(
            {
                "title": entry.get("title") or url,
                "url": url,
                "outlet": urlparse(url).hostname or "",
            }
        )
    if not items:
        return None
    return {"type": "further_reading", "items": items}


def _reading_time(sections: list[dict]) -> int:
    words = 0
    for section in sections:
        words += len(section.get("text", "").split())
        words += sum(len(point.split()) for point in section.get("items", []) if isinstance(point, str))
    return max(1, round(words / 220))


def _writer_seed(condensed: list[str]) -> str:
    return (
        "Source items for this story (trusted feed). Research to deepen the story — "
        "fetch these URLs to recover links they contain, search for primary sources — "
        "then you will write the post:\n\n" + "\n\n---\n\n".join(condensed)
    )


def _demote(story: Story, reason: str) -> None:
    story.status = "aggregated"
    story.triage_decision = "aggregate"
    story.triage_reason = reason


async def write_posts(session: AsyncSession) -> int:
    """The main model's agentic write (spec §7): work the ranked candidates best-first
    until the wall-clock budget is spent (max_writes_per_run is a hard safety cap).

    Two passes, batched by model role so the GPU never swaps mid-story: first the fast
    model condenses every candidate's long sources (once — the same text seeds the
    writer's research and its draft), then each story gets one main-model tool loop
    with research tools and editorial authority (write or demote)."""
    stories = (
        (
            await session.execute(
                select(Story)
                .where(Story.status == "triaged", Story.triage_decision == "write")
                .order_by(Story.rank_score.desc().nulls_last(), Story.last_item_at.desc())
                .limit(settings.max_writes_per_run)
            )
        )
        .scalars()
        .all()
    )

    # Pass 1 (fast, batched): condense long sources once per story.
    prepared: dict[int, tuple[list[SourceItem], str, int]] = {}
    for story in stories:
        items = await _story_items(session, story)
        with llm_context(story_id=story.id):
            condensed = [
                f"[{item.source.name}] {item.title}\nURL: {item.url}\n"
                f"{await _condensed_source(item)}"
                for item in items
            ]
        source_chars = sum(len(_plain_text(item)) for item in items)
        prepared[story.id] = (items, _writer_seed(condensed), source_chars)

    # Pass 2 (main): one agentic loop per story.
    written = 0
    deadline = time.monotonic() + settings.write_budget_seconds
    for story in stories:
        if time.monotonic() > deadline:
            log.info("write stage hit wall-clock budget (%ds)", settings.write_budget_seconds)
            break
        items, seed, source_chars = prepared[story.id]
        try:
            with llm_context(stage="write", story_id=story.id):
                outcome = await run_writer_loop(WRITER_AGENT_SYSTEM, seed)
        except Exception as exc:
            log.warning("Writer loop failed for story %d: %s", story.id, exc)
            continue
        story.research_notes = {"fetched": outcome.fetch_log, "notes": outcome.notes}

        if outcome.decision != "write" or outcome.draft is None:
            _demote(story, outcome.reason or "writer demoted")
            await session.commit()
            log.info("Story %d demoted by writer: %s", story.id, outcome.reason)
            continue

        # Deterministic thin-gate backstop: a bare caption that even research couldn't
        # expand is aggregated, not written — reliable where model judgment wasn't.
        available_chars = source_chars + outcome.gathered_chars
        if available_chars < settings.min_write_chars:
            _demote(story, f"Too thin to write ({available_chars} chars after research)")
            await session.commit()
            log.info("Story %d aggregated: only %d chars after research", story.id, available_chars)
            continue

        item_urls = {item.url for item in items}
        sections = [s.model_dump() for s in outcome.draft.sections]
        sections.append(_sources_section(items))
        further = _further_reading_section(outcome.fetch_log, item_urls)
        if further:
            sections.append(further)
        session.add(
            Post(
                story_id=story.id,
                kind="feature",
                title=outcome.draft.title,
                summary=outcome.draft.summary,
                difficulty=outcome.draft.difficulty,
                topics=outcome.draft.topics,
                sections=sections,
                reading_time_minutes=_reading_time(sections),
                model_used=gateway.model_for("main"),
            )
        )
        story.status = "written"
        await session.commit()
        written += 1
        log.info("Wrote feature for story %d: %s", story.id, outcome.draft.title)
    return written


# --- Orchestrator ----------------------------------------------------------------


async def _prune_llm_calls(session: AsyncSession) -> None:
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=settings.llm_log_retention_days)
    await session.execute(delete(LlmCall).where(LlmCall.created_at < cutoff))
    await session.commit()


@app.task(name="episteme.run_pipeline")
async def run_pipeline() -> None:
    """Full nightly pass, batched by model role so each model loads once. Each pass
    records a PipelineRun row (per-stage counters, outcome) for the admin view."""
    async with SessionLocal() as session:
        run = PipelineRun()
        session.add(run)
        await session.commit()

        if not await gateway.is_available():
            log.warning("LLM endpoint %s unavailable; skipping pipeline run", settings.llm_base_url)
            run.status = "skipped"
            run.error = f"LLM endpoint {settings.llm_base_url} unavailable"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            return

        errors: list[str] = []
        try:
            for name, stage in (
                ("embed", embed_new_items),
                ("cluster", cluster_items),
                ("triage", triage_stories),
                ("write", write_posts),
            ):
                try:
                    count = await stage(session)
                    log.info("Pipeline stage %s: %d processed", name, count)
                    run.stages = {**run.stages, name: count}
                except LLMError as exc:
                    log.warning("Pipeline stage %s skipped: %s", name, exc)
                    errors.append(f"{name}: {exc}")
                await session.commit()
        except Exception as exc:
            # Unexpected failure: don't leave the run row stuck at "running".
            await session.rollback()
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            raise

        run.status = "failed" if errors else "succeeded"
        run.error = "; ".join(errors) or None
        run.finished_at = datetime.now(UTC)
        await session.commit()
        await _prune_llm_calls(session)


@app.periodic(cron=settings.pipeline_cron)
@app.task(name="episteme.scheduled_pipeline")
async def scheduled_pipeline(timestamp: int) -> None:
    await run_pipeline.defer_async()
