"""The scoring pass: profile + posts in, `Post.affinity_score` out.

Lives here rather than in `worker/pipeline.py` because both sides need it — the
nightly `score` stage runs it, and recording feedback defers it — and the web
process must never have to import the worker to make the feed reflect a click.
`pipeline.score_posts` is a thin stage wrapper over `rescore`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Post, Source, SourceItem, Story
from . import profile as profile_service
from . import scorers
from . import topics as topics_service

log = logging.getLogger("episteme.recommend.scoring")

# One name, so the lock a click takes is the lock the next click is refused on.
RESCORE_LOCK = "rescore"


async def rescore(
    session: AsyncSession, limit: int | None = None, post_id: int | None = None
) -> int:
    """Score published posts against the interest profile (spec §8).

    Rescores everything rather than only what changed: the profile is a single
    replayed object, so one signal can move every post's standing, and at a single
    user's corpus size a full pass is an in-memory loop with no LLM involvement.
    Partial rescoring would buy little and could leave the feed ordered by a
    mixture of two different profiles.

    Freshness is deliberately not applied here — it belongs to the feed query, so
    a stored score never has to be rewritten just because time passed.
    """
    state = await profile_service.load(session)
    # One vocabulary map for the whole pass: stories and posts store topic
    # LABELS, weights are keyed by the topic's permanent slug, and the two part
    # company as soon as a topic is renamed.
    slugs = await topics_service.slug_index(session)
    query = select(Post, Story).join(Story, Post.story_id == Story.id)
    if post_id is not None:
        query = query.where(Post.id == post_id)
    else:
        query = query.where(Post.status == "published")
        if limit is not None:
            query = query.limit(limit)
    rows = (await session.execute(query)).all()
    if not rows:
        return 0

    # Contributing sources per story, in one query rather than one per post.
    source_rows = (
        await session.execute(
            select(SourceItem.story_id, Source.id, Source.credibility_rating)
            .join(Source, Source.id == SourceItem.source_id)
            .where(SourceItem.story_id.in_([story.id for _, story in rows]))
            .distinct()
        )
    ).all()
    by_story: dict[int, list[tuple[int, float]]] = {}
    for row in source_rows:
        by_story.setdefault(row.story_id, []).append((row.id, row.credibility_rating))

    now = datetime.now(UTC)
    for post, story in rows:
        sources = by_story.get(story.id, [])
        candidate = scorers.Candidate(
            post_id=post.id,
            kind=post.kind,
            embedding=list(story.centroid) if story.centroid is not None else None,
            # An aggregate card has no content columns of its own — it renders from
            # its story, so it is scored from its story's topics.
            topic_slugs=[
                topics_service.slug_for(label, slugs)
                for label in (list(post.topics or []) or list(story.topics or []))
            ],
            source_ids=[source_id for source_id, _ in sources],
            difficulty=post.difficulty,
            # QA's verdict when there is one; otherwise triage's ranking of the
            # story, which is the only quality signal an aggregate card ever has.
            quality_score=(
                post.quality_score if post.quality_score is not None else story.rank_score
            ),
            credibility=(sum(rating for _, rating in sources) / len(sources) if sources else 0.5),
        )
        total, components = scorers.score_candidate(candidate, state)
        post.affinity_score = total
        post.score_components = components
        post.scored_at = now
    await session.commit()
    log.info("Scored %d posts against %d profile signals", len(rows), state.event_count)
    return len(rows)


async def defer_rescore() -> None:
    """Queue a rescore after feedback, coalescing bursts into one pass.

    Deferred rather than run inline: a full pass is a real CPU cost (1024-dimension
    dot products in pure Python) and has no business inside a button click.

    Coalesced because reading is bursty. Every signal moves the whole profile, so
    every signal needs a full rescore — but ten clicks in a minute need ONE, not
    ten identical passes. The job is scheduled `rescore_debounce_seconds` out under
    a queueing lock, and procrastinate's partial unique index (one row per
    `queueing_lock` while status is `todo`) makes the database refuse the duplicate:
    later clicks in the window ride on the first one's job. The lock is released the
    moment a worker picks the job up, so a signal arriving mid-pass — which that
    pass may already have read past — correctly queues the next one.

    Leading-edge and fixed-width, deliberately: the window starts at the first
    signal and is never extended, so continued clicking cannot starve the rescore.
    The reader's own view is not waiting on this in any case — `rebuild` has already
    committed the new profile, and only the stored `affinity_score` ordering lags,
    by at most the window.

    Best-effort throughout: an unreachable queue must never lose the signal that was
    just recorded, and the nightly `score` stage catches up regardless.
    """
    try:
        from procrastinate.exceptions import AlreadyEnqueued

        from ..worker.app import app as job_app

        options: dict = {}
        if settings.rescore_debounce_seconds > 0:
            options = {
                "queueing_lock": RESCORE_LOCK,
                "schedule_in": {"seconds": settings.rescore_debounce_seconds},
            }
        async with job_app.open_async():
            try:
                await job_app.configure_task("episteme.pipeline_stage", **options).defer_async(
                    stage="score"
                )
            except AlreadyEnqueued:
                log.debug("Rescore already pending; this signal rides on it")
    except Exception as exc:  # noqa: BLE001 - recording the signal is what matters
        log.warning("Could not defer rescore after feedback: %s", exc)
