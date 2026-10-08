"""Filing: a finished post, handed over rather than generated (0046).

`ingest/manual.py` is the precedent. It mints a `Story` and a `SourceItem`
outside the normal path so a reader-requested URL has somewhere to live, then
gets out of the way and lets the ordinary writer, QA and feed handle it. This
module does the same for a correspondent, one stage further along: there is
nothing left to generate, so it mints the post as well.

**What core knows and what it does not.** A period key is opaque here. Whether a
correspondent's period is a week, a weekday or a fortnight is its own decision,
and so is whether one story carries one item or four. Core enforces three things
and nothing else:

- a filed story carries at least one item with a URL, which is what keeps
  `item_count > 0` honest and makes the post accountable through
  `/post/{id}/provenance`. A correspondent that reads nothing is generating
  content, which is the writer's job;
- the post stores an `href` and carries no body (0047), because a filed post's
  content lives on the correspondent's page, not at `/post/{id}`;
- re-filing a period **upserts**. A menu corrected on Tuesday morning must not
  become two Tuesdays.

**Identity comes from the items, not from a column.** There is no `period_key`
on `stories`, and there does not need to be one: `source_items.hash` is unique,
so re-filing with the same item keys finds the same rows, and the story they
already belong to is the story for that period. That is the same trick
`manual.py` uses to notice it has seen a URL before, applied to a producer-chosen
identity instead of a canonical URL.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm import gateway
from ..llm.gateway import LLMError
from ..models import Correspondent, Post, Source, SourceItem, Story

log = logging.getLogger("episteme.correspondents.filing")


class FilingError(Exception):
    """The correspondent handed over something that cannot become a post."""


@dataclass(frozen=True)
class FiledItem:
    """One thing the correspondent read, in the shape `SourceItem` stores.

    `key` is the producer's identity for this item and is hashed into
    `SourceItem.hash` (0046). Matsedel's looks like
    `Matsedel/koppargrillen/2026w35`. It exists because a correspondent that
    re-reads one unchanging URL every week cannot key on the URL: the column is
    unique, and `canonicalize_url` drops the fragment that might have
    distinguished them.
    """

    key: str
    url: str
    title: str | None = None
    text: str | None = None
    published_at: datetime | None = None
    media_refs: list[Any] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()


async def file_post(
    session: AsyncSession,
    *,
    source: Source,
    items: Sequence[FiledItem],
    title: str,
    href: str,
    summary: str | None = None,
    topics: Sequence[str] = (),
    publish_at: datetime | None = None,
    expires_at: datetime | None = None,
    kind: str = "filed",
) -> Post:
    """File one finished post, creating or updating the story behind it.

    Returns the post. The caller commits: a correspondent filing a week of posts
    wants five of them or none, not four and a traceback.

    `publish_at` is when the post becomes visible and `expires_at` when it stops
    being, both NULL for no bound. Together they are what lets a correspondent
    read a week once and create every weekday's post from that single read, each
    appearing on its own morning and leaving when it stops being true.
    """
    if not items:
        raise FilingError("a filed story needs at least one item; nothing was handed over")
    missing = [item.key for item in items if not item.url]
    if missing:
        raise FilingError(f"filed items need a URL: {missing}")
    hashes = [item.hash for item in items]
    if len(set(hashes)) != len(hashes):
        raise FilingError("two filed items share one key; keys identify an item")
    # Last of the guards, and the only one that reads the database.
    await _require_correspondent_source(session, source)

    now = datetime.now(UTC)
    rows = await _upsert_items(session, source, items, now)
    story = await _story_for(session, rows, now)
    for row in rows:
        row.story_id = story.id
    story.item_count = len(rows)
    story.first_item_at = min(_item_time(row, now) for row in rows)
    story.last_item_at = max(_item_time(row, now) for row in rows)
    story.topics = list(topics)
    await session.flush()

    await _embed_story(session, story, rows)

    post = (
        (
            await session.execute(
                select(Post).where(Post.story_id == story.id, Post.status == "published")
            )
        )
        .scalars()
        .first()
    )
    if post is None:
        post = Post(story_id=story.id, kind=kind)
        session.add(post)
    # Updated in place rather than archived and replaced. A correction is the same
    # post saying something truer, and archiving would change its id (breaking any
    # link already given out) and leave a row the prune deletes 30 days later. A
    # rewrite archives because the OLD article is a thing someone may want to
    # compare against; a wrong menu is not.
    post.title = title
    post.summary = summary
    post.topics = list(topics)
    post.href = href
    post.publish_at = publish_at
    post.expires_at = expires_at
    # No body, and the database is what enforces that (0047). A filed post's
    # content lives on the correspondent's page.
    post.sections = []
    await session.flush()
    log.info(
        "Filed post %s for story %d (%d item(s), publish_at=%s)",
        post.id,
        story.id,
        len(rows),
        publish_at,
    )
    return post


async def _require_correspondent_source(session: AsyncSession, source: Source) -> str:
    """`sources.type_name` IS the correspondent's slug, and this is what makes
    that true rather than customary.

    A filed post's card names its correspondent, and nothing on the post points
    at one: the card walks post -> story -> primary item -> source and reads the
    slug off `type_name` (0046). That is one identifier doing two jobs, so it is
    checked at the one moment the two are bound - here, where a source is first
    used for filing. A convention nobody checks is a convention that drifts, and
    the drift would surface as a card with no byline weeks later.

    Checked against the `correspondents` TABLE, not the registry: a row with no
    plugin is exactly what an external correspondent is (0046).
    """
    slug = (
        (
            await session.execute(
                select(Correspondent.slug).where(Correspondent.slug == source.type_name)
            )
        )
        .scalars()
        .first()
    )
    if slug is None:
        raise FilingError(
            f"source {source.id} has type_name {source.type_name!r}, which is not a "
            f"correspondent slug. A filed post's card reads its byline off that "
            f"column, so the two have to be the same string."
        )
    return slug


def _item_time(row: SourceItem, fallback: datetime) -> datetime:
    return row.published_at or fallback


async def _upsert_items(
    session: AsyncSession, source: Source, items: Sequence[FiledItem], now: datetime
) -> list[SourceItem]:
    """The items for this period, created or updated in place by their hash.

    `file_post` has already refused duplicate keys; this can upsert.
    """
    hashes = [item.hash for item in items]
    existing = {
        row.hash: row
        for row in (
            await session.execute(select(SourceItem).where(SourceItem.hash.in_(hashes)))
        ).scalars()
    }
    rows: list[SourceItem] = []
    for item in items:
        row = existing.get(item.hash)
        if row is None:
            row = SourceItem(source_id=source.id, hash=item.hash)
            session.add(row)
        elif row.source_id != source.id:
            # A key that already belongs to another source is a collision, not a
            # correction, and silently re-homing the row would move it out from
            # under whatever filed it first.
            raise FilingError(f"item key {item.key!r} already belongs to source {row.source_id}")
        row.url = item.url
        row.title = item.title or item.url
        row.extracted_text = item.text
        row.media_refs = list(item.media_refs)
        row.published_at = item.published_at or row.published_at or now
        row.fetch_status = "extracted"
        # A corrected item is different text, so its old vector is wrong. Cleared
        # rather than kept: the `embed` stage heals it either way, and
        # `_embed_story` below takes the fast path when the server answers.
        row.embedding = None
        rows.append(row)
    await session.flush()
    return rows


async def _story_for(session: AsyncSession, rows: Sequence[SourceItem], now: datetime) -> Story:
    """The story these items already belong to, or a new one.

    A re-file finds the story through its items, which is why no `period_key`
    column exists. Items that disagree about their story mean the correspondent
    changed how it groups periods, and merging them silently would strand a post.
    """
    story_ids = {row.story_id for row in rows if row.story_id is not None}
    if len(story_ids) > 1:
        raise FilingError(
            f"filed items already belong to different stories {sorted(story_ids)}; "
            "the period grouping changed and the old posts need retiring first"
        )
    if story_ids:
        story = await session.get(Story, story_ids.pop())
        if story is not None:
            return story
    story = Story(
        # Neither the pipeline's clustering nor a reader request. Recording
        # `ingest` would say the clusterer made this, which it did not - the same
        # objection that kept `status` off `triaged` (0046).
        origin="correspondent",
        # A terminal state. `triage_pending()` matches `new` and `write_pending()`
        # matches `triaged`, both by name, so a filed story is out of every stage
        # by construction rather than by a filter someone has to remember.
        status="filed",
        topics=[],
        item_count=0,
        first_item_at=now,
        last_item_at=now,
    )
    session.add(story)
    await session.flush()
    return story


async def _embed_story(session: AsyncSession, story: Story, rows: Sequence[SourceItem]) -> None:
    """Best effort, exactly as `manual.ingest_url` is.

    A missing vector costs this story its place in semantic search; it does not
    stop the post existing, and filing must not fail because the embed server is
    down. The `embed` stage heals the ITEMS on its next pass. The story centroid
    is set here or not at all, because nothing recomputes it for a cluster that
    never grows - `cluster_pending()` only looks at items with no story.
    """
    texts = [f"{row.title}\n\n{row.extracted_text or ''}".strip() for row in rows]
    try:
        vectors = await gateway.embed(texts)
    except LLMError as exc:
        log.warning("No embeddings for filed story %d: %s", story.id, exc)
        return
    for row, vector in zip(rows, vectors, strict=True):
        row.embedding = vector
    story.centroid = _mean_unit(vectors)
    await session.flush()


def _mean_unit(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Centroid of unit vectors, re-normalized. `pipeline.update_centroid` is the
    incremental form of the same thing; a filed story is built in one go, so this
    is the batch form rather than a fold over the incremental one."""
    count = len(vectors)
    merged = [sum(values) / count for values in zip(*vectors, strict=True)]
    norm = sum(x * x for x in merged) ** 0.5 or 1.0
    return [x / norm for x in merged]
