"""LLM processing pipeline (spec §7): embed -> cluster -> triage -> write -> qa.

Stages are plain async functions wrapped in procrastinate tasks, so the nightly
orchestrator calls them in role-batched order (all fast-model work, then all
main-model work — one model load each), while individual stages stay manually
deferrable for testing.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
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
from .control import pause_requested
from .qa import qa_posts

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


async def embed_new_items(session: AsyncSession, limit: int | None = None) -> int:
    query = (
        select(SourceItem).where(SourceItem.embedding.is_(None)).order_by(SourceItem.id)
    )
    if limit is not None:
        query = query.limit(limit)
    items = (await session.execute(query)).scalars().all()
    embedded = 0
    for start in range(0, len(items), settings.embed_batch_size):
        if await pause_requested(session):
            log.info("embed stage pausing after %d items", embedded)
            break
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


async def cluster_items(session: AsyncSession, limit: int | None = None) -> int:
    # No pause check: pure DB work, done in seconds — nothing worth interrupting.
    query = (
        select(SourceItem)
        .where(SourceItem.embedding.is_not(None), SourceItem.story_id.is_(None))
        .order_by(SourceItem.published_at.asc().nulls_last(), SourceItem.id)
    )
    if limit is not None:
        query = query.limit(limit)
    items = (await session.execute(query)).scalars().all()
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


async def triage_stories(
    session: AsyncSession, limit: int | None = None, story_id: int | None = None
) -> int:
    if story_id is not None:
        # Explicit target: re-triage regardless of current status (testing lever).
        query = select(Story).where(Story.id == story_id)
    else:
        query = select(Story).where(Story.status == "new", Story.item_count > 0)
        if limit is not None:
            query = query.limit(limit)
    stories = (await session.execute(query)).scalars().all()
    triaged = 0
    for story in stories:
        if await pause_requested(session):
            log.info("triage stage pausing after %d stories", triaged)
            break
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
        # The verdict decides whether feed content exists: an aggregate verdict
        # mints the story's card post; a (re-triage) skip retires it. A write
        # verdict leaves any existing card up until the feature supersedes it.
        if result.decision == "aggregate":
            await ensure_aggregate_post(session, story)
        elif result.decision == "skip":
            await _retire_aggregate_post(session, story.id)
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


def _further_reading_section(
    fetch_log: list[dict], item_urls: set[str], selected: list[str]
) -> dict | None:
    """Built by code from the research agent's fetch log — the URLs it actually
    retrieved, never model free-text — so enrichment links can't be hallucinated.
    The writer's `further_reading_urls` narrows the log to the pages it judged
    relevant (dead-end fetches stay out), but only fetch-log membership puts a URL
    on the page. Excludes URLs already in the DB-built sources section."""
    from urllib.parse import urlparse

    wanted = {u.rstrip("/") for u in selected}
    seen: set[str] = set()
    items = []
    for entry in fetch_log:
        url = entry.get("url", "")
        if not url or url in item_urls or url in seen or url.rstrip("/") not in wanted:
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


async def _stamp_post_calls(session: AsyncSession, story_id: int, post_id: int) -> None:
    """Attribute the write-side llm_calls that just produced a post to it. They
    run before the post row exists, so they land with post_id NULL and are
    stamped here right after the post commits. Scoops any unattributed
    condense/write rows for the story — including a prior failed attempt's,
    which is the same generation effort. Best-effort like all call logging."""
    try:
        await session.execute(
            update(LlmCall)
            .where(
                LlmCall.story_id == story_id,
                LlmCall.post_id.is_(None),
                LlmCall.stage.in_(("condense", "write")),
            )
            .values(post_id=post_id)
        )
        await session.commit()
    except Exception as exc:
        log.debug("stamping llm_calls for post %d failed: %s", post_id, exc)


async def ensure_aggregate_post(session: AsyncSession, story: Story) -> None:
    """All feed content is a post: an aggregated story is represented by an
    identity-only row (kind="aggregate" — no stored content, the card renders
    from the story's items at read time). Created only when the story has no
    published post, so a card never coexists with a live feature (at most one
    published post per story)."""
    published = (
        await session.execute(
            select(Post.id).where(Post.story_id == story.id, Post.status == "published").limit(1)
        )
    ).scalar_one_or_none()
    if published is None:
        session.add(Post(story_id=story.id, kind="aggregate"))


async def _retire_aggregate_post(session: AsyncSession, story_id: int) -> None:
    await session.execute(
        update(Post)
        .where(Post.story_id == story_id, Post.kind == "aggregate", Post.status == "published")
        .values(status="archived", archived_at=datetime.now(UTC))
    )


async def _demote(session: AsyncSession, story: Story, reason: str) -> None:
    story.status = "aggregated"
    story.triage_decision = "aggregate"
    story.triage_reason = reason
    await ensure_aggregate_post(session, story)


async def write_posts(
    session: AsyncSession, limit: int | None = None, story_id: int | None = None
) -> int:
    """The main model's agentic write (spec §7): work the ranked candidates best-first
    until the wall-clock budget is spent (max_writes_per_run is a hard safety cap;
    `limit` overrides it for partial/test runs).

    An explicit `story_id` rewrites that story regardless of its status — existing
    published posts for it are archived when the new draft lands.

    Two passes, batched by model role so the GPU never swaps mid-story: first the fast
    model condenses every candidate's long sources (once — the same text seeds the
    writer's research and its draft), then each story gets one main-model tool loop
    with research tools and editorial authority (write or demote)."""
    if story_id is not None:
        query = select(Story).where(Story.id == story_id)
    else:
        query = (
            select(Story)
            .where(Story.status == "triaged", Story.triage_decision == "write")
            .order_by(Story.rank_score.desc().nulls_last(), Story.last_item_at.desc())
            .limit(limit if limit is not None else settings.max_writes_per_run)
        )
    stories = (await session.execute(query)).scalars().all()

    # Pass 1 (fast, batched): condense long sources once per story.
    prepared: dict[int, tuple[list[SourceItem], str, int]] = {}
    for story in stories:
        if await pause_requested(session):
            # Don't start main-model work on a pause: condense output is cheap to
            # redo next run, the write pass is minutes of GPU per story.
            log.info("write stage pausing before main-model pass (%d condensed)", len(prepared))
            return 0
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
        if await pause_requested(session):
            log.info("write stage pausing after %d posts", written)
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
            await _demote(session, story, outcome.reason or "writer demoted")
            await session.commit()
            log.info("Story %d demoted by writer: %s", story.id, outcome.reason)
            continue

        # Deterministic thin-gate backstop: a bare caption that even research couldn't
        # expand is aggregated, not written — reliable where model judgment wasn't.
        available_chars = source_chars + outcome.gathered_chars
        if available_chars < settings.min_write_chars:
            await _demote(session, story, f"Too thin to write ({available_chars} chars after research)")
            await session.commit()
            log.info("Story %d aggregated: only %d chars after research", story.id, available_chars)
            continue

        item_urls = {item.url for item in items}
        sections = [s.model_dump() for s in outcome.draft.sections]
        sections.append(_sources_section(items))
        further = _further_reading_section(
            outcome.fetch_log, item_urls, outcome.draft.further_reading_urls
        )
        if further:
            sections.append(further)
        # The new feature supersedes whatever the story published before — an
        # older feature (rewrite) or its aggregate card. One published post per story.
        await session.execute(
            update(Post)
            .where(Post.story_id == story.id, Post.status == "published")
            .values(status="archived", archived_at=datetime.now(UTC))
        )
        post = Post(
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
        session.add(post)
        await session.flush()
        new_post_id = post.id
        story.status = "written"
        await session.commit()
        await _stamp_post_calls(session, story.id, new_post_id)
        written += 1
        log.info("Wrote feature for story %d: %s", story.id, outcome.draft.title)
    return written


# --- Orchestrator ----------------------------------------------------------------


async def _prune_expired(session: AsyncSession) -> None:
    """Retention: drop llm_calls past the window, and archived posts whose
    replacement is old enough that the before/after comparison is over —
    a superseded version and its provenance age out together."""
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=settings.llm_log_retention_days)
    await session.execute(delete(LlmCall).where(LlmCall.created_at < cutoff))
    await session.execute(
        delete(Post).where(Post.status == "archived", Post.archived_at < cutoff)
    )
    await session.commit()


@app.task(name="episteme.run_pipeline")
async def run_pipeline() -> None:
    """Full nightly pass, batched by model role so each model loads once. Each pass
    records a PipelineRun row (per-stage counters, outcome) for the admin view."""
    async with SessionLocal() as session:
        run = PipelineRun()
        session.add(run)
        await session.commit()

        if await pause_requested(session):
            log.info("Pipeline paused; run recorded and skipped (resume via /api/pipeline/resume)")
            run.status = "paused"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            await gateway.unload_models()
            return

        if not await gateway.is_available():
            log.warning("LLM endpoint %s unavailable; skipping pipeline run", settings.llm_base_url)
            run.status = "skipped"
            run.error = f"LLM endpoint {settings.llm_base_url} unavailable"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            return

        paused = False
        errors: list[str] = []
        # Track counters locally and only ever *assign* run.stages: a stage's
        # rollback() expires every object in the shared session, and reading an
        # expired attribute on an AsyncSession object raises MissingGreenlet.
        stages: dict[str, int] = {}
        try:
            for name, stage in (
                ("embed", embed_new_items),
                ("cluster", cluster_items),
                ("triage", triage_stories),
                ("write", write_posts),
                ("qa", qa_posts),  # stays on `main`, so no model swap after write
            ):
                try:
                    count = await stage(session)
                    log.info("Pipeline stage %s: %d processed", name, count)
                    stages[name] = count
                    run.stages = dict(stages)
                except LLMError as exc:
                    log.warning("Pipeline stage %s skipped: %s", name, exc)
                    errors.append(f"{name}: {exc}")
                await session.commit()
                if await pause_requested(session):
                    # Stages stop at unit boundaries; leftover work is picked up by
                    # the next run (data-driven selection), so just stop cleanly.
                    paused = True
                    break
        except Exception as exc:
            # Unexpected failure: don't leave the run row stuck at "running".
            await session.rollback()
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            raise

        run.status = "paused" if paused else ("failed" if errors else "succeeded")
        run.error = "; ".join(errors) or None
        run.finished_at = datetime.now(UTC)
        await session.commit()
        if paused:  # free VRAM for whatever prompted the pause
            await gateway.unload_models()
        await _prune_expired(session)


"""Single-stage runs: stages are data-driven (each selects whatever rows are
still unprocessed), so any stage can run standalone and it picks up from
previous work — write without re-triaging, qa without writing. STAGE_PARAMS is
the contract the API validates defer params against."""
STAGE_RUNNERS = {
    "embed": embed_new_items,
    "cluster": cluster_items,
    "triage": triage_stories,
    "write": write_posts,
    "qa": qa_posts,
}
STAGE_PARAMS: dict[str, frozenset[str]] = {
    "embed": frozenset({"limit"}),
    "cluster": frozenset({"limit"}),
    "triage": frozenset({"limit", "story_id"}),
    "write": frozenset({"limit", "story_id"}),
    "qa": frozenset({"limit", "post_id"}),
}


@app.task(name="episteme.pipeline_stage")
async def pipeline_stage(
    stage: str,
    limit: int | None = None,
    story_id: int | None = None,
    post_id: int | None = None,
) -> None:
    """One pipeline stage on its own, with optional caps/targeting — the testing
    lever behind POST /api/jobs/defer/{stage}. Records a PipelineRun row like the
    orchestrator so partial runs show up in the admin view."""
    runner = STAGE_RUNNERS[stage]
    kwargs = {
        key: value
        for key, value in {"limit": limit, "story_id": story_id, "post_id": post_id}.items()
        if value is not None and key in STAGE_PARAMS[stage]
    }
    async with SessionLocal() as session:
        run = PipelineRun()
        session.add(run)
        await session.commit()

        if stage != "cluster" and not await gateway.is_available():
            run.status = "skipped"
            run.error = f"LLM endpoint {settings.llm_base_url} unavailable"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            return

        try:
            count = await runner(session, **kwargs)
        except Exception as exc:
            await session.rollback()
            run.status = "failed"
            run.error = f"{stage}: {type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            raise
        run.stages = {stage: count}
        paused = await pause_requested(session)
        run.status = "paused" if paused else "succeeded"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        if paused:
            await gateway.unload_models()


@app.periodic(cron=settings.pipeline_cron)
@app.task(name="episteme.scheduled_pipeline")
async def scheduled_pipeline(timestamp: int) -> None:
    await run_pipeline.defer_async()
