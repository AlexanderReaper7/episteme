"""LLM processing pipeline (spec §7): embed -> cluster -> triage -> write.

Stages are plain async functions wrapped in procrastinate tasks, so the nightly
orchestrator calls them in role-batched order (all fast-model work, then all
writer work — one model load each), while individual stages stay manually
deferrable for testing.
"""

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..llm import LLMError, gateway
from ..llm.prompts import SUMMARIZE_SYSTEM, TRIAGE_SYSTEM, WRITER_SYSTEM
from ..llm.schemas import ArticleDraft, SourceSummary, TriageResult
from ..models import Article, SourceItem, Story
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
            result = await gateway.complete_json(
                "fast", TRIAGE_SYSTEM, f"Story items:\n\n{digest}", TriageResult
            )
        except LLMError as exc:
            log.warning("Triage failed for story %d: %s", story.id, exc)
            continue
        story.triage_decision = result.decision
        story.triage_reason = result.reason
        story.topics = result.topics
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


def _reading_time(sections: list[dict]) -> int:
    words = 0
    for section in sections:
        words += len(section.get("text", "").split())
        words += sum(len(point.split()) for point in section.get("items", []) if isinstance(point, str))
    return max(1, round(words / 220))


async def write_articles(session: AsyncSession) -> int:
    stories = (
        (
            await session.execute(
                select(Story)
                .where(Story.status == "triaged", Story.triage_decision == "write")
                .order_by(Story.last_item_at.desc())
                .limit(settings.max_writes_per_run)
            )
        )
        .scalars()
        .all()
    )
    written = 0
    for story in stories:
        items = await _story_items(session, story)
        try:
            condensed = [
                f"[{item.source.name}] {item.title}\n{await _condensed_source(item)}"
                for item in items
            ]
            draft = await gateway.complete_json(
                "writer",
                WRITER_SYSTEM,
                "Source material:\n\n" + "\n\n---\n\n".join(condensed),
                ArticleDraft,
                temperature=0.4,
            )
        except LLMError as exc:
            log.warning("Writing failed for story %d: %s", story.id, exc)
            continue
        sections = [s.model_dump() for s in draft.sections]
        sections.append(_sources_section(items))
        session.add(
            Article(
                story_id=story.id,
                title=draft.title,
                summary=draft.summary,
                difficulty=draft.difficulty,
                topics=draft.topics,
                sections=sections,
                reading_time_minutes=_reading_time(sections),
                model_used=gateway.model_for("writer"),
            )
        )
        story.status = "written"
        await session.commit()
        written += 1
        log.info("Wrote article for story %d: %s", story.id, draft.title)
    return written


# --- Orchestrator ----------------------------------------------------------------


@app.task(name="episteme.run_pipeline")
async def run_pipeline() -> None:
    """Full nightly pass, batched by model role so each model loads once."""
    if not await gateway.is_available():
        log.warning("LLM endpoint %s unavailable; skipping pipeline run", settings.llm_base_url)
        return
    async with SessionLocal() as session:
        for name, stage in (
            ("embed", embed_new_items),
            ("cluster", cluster_items),
            ("triage", triage_stories),
            ("write", write_articles),
        ):
            try:
                count = await stage(session)
                log.info("Pipeline stage %s: %d processed", name, count)
            except LLMError as exc:
                log.warning("Pipeline stage %s skipped: %s", name, exc)


@app.periodic(cron=settings.pipeline_cron)
@app.task(name="episteme.scheduled_pipeline")
async def scheduled_pipeline(timestamp: int) -> None:
    await run_pipeline.defer_async()
