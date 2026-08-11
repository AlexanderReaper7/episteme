"""What each pipeline stage would pick up if it ran right now.

One definition per stage, imported by BOTH the stage that consumes the rows and
the admin page that counts them. The page used to state what a stage *takes*
("new source items") and leave the only question that matters - is anything
waiting? - to be answered by pressing the button and reading the result line.

Written down once rather than twice on purpose. A count that merely resembled the
stage's own query would agree with it until the first time someone edited one of
them, and a number that is wrong in a way nothing can detect is worse than no
number: it is the same button press, taken on a false premise.

`narrate` is deliberately absent. Its pending set is every published feature post
whose rendered script hash differs from the audio already stored for the chosen
voice, which means building the script for every post - not a count, a pass. It
is the one stage whose "due" is not a predicate over indexed columns.
"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Post, SourceItem, Story
from ..recommend import blocks
from ..recommend.profile import ProfileState


def embed_pending() -> Select:
    return select(SourceItem).where(SourceItem.embedding.is_(None))


def cluster_pending() -> Select:
    return select(SourceItem).where(
        SourceItem.embedding.is_not(None), SourceItem.story_id.is_(None)
    )


def triage_pending() -> Select:
    return select(Story).where(Story.status == "new", Story.item_count > 0)


def write_pending(profile: ProfileState) -> Select:
    """Blocked stories are excluded rather than demoted: the block is a display
    decision and must not destroy pipeline state, so it takes the profile."""
    return select(Story).where(
        Story.status == "triaged",
        Story.triage_decision == "write",
        *blocks.filters(profile, Story.id),
    )


def qa_pending() -> Select:
    """Aggregate posts are identity-only cards - nothing generated to review."""
    return select(Post.id).where(
        Post.quality_score.is_(None),
        Post.status == "published",
        Post.kind != "aggregate",
    )


def score_pending() -> Select:
    """Every published post, every time: the profile is one replayed object, so a
    single signal can move everything's standing (see recommend.scoring)."""
    return select(Post.id).where(Post.status == "published")


async def count(session: AsyncSession, query: Select) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    )


async def stage_backlog(session: AsyncSession, profile: ProfileState) -> dict[str, dict]:
    """Per stage: how many rows are waiting, and whether pressing the button would
    act on them at all.

    `blocked` is not the same as a zero count and must not be rendered as one.
    `qa` and `narrate` return 0 outright when their feature flag is off unless a
    post id was named, so "9 due" beside a button that will do nothing is exactly
    the false premise this module exists to prevent.
    """
    counts = {
        "embed": await count(session, embed_pending()),
        "cluster": await count(session, cluster_pending()),
        "triage": await count(session, triage_pending()),
        "write": await count(session, write_pending(profile)),
        "qa": await count(session, qa_pending()),
        "score": await count(session, score_pending()),
    }
    blocked = {
        "qa": None if settings.qa_enabled else "qa_enabled is off",
        "narrate": None if settings.tts_enabled else "tts_enabled is off",
    }
    return {
        stage: {"due": counts.get(stage), "blocked": blocked.get(stage)}
        for stage in ("embed", "cluster", "triage", "write", "qa", "narrate", "score")
    }
