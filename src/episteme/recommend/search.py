"""Finding a post you half-remember.

Two legs, because the two failure modes are opposite. A literal `ILIKE` finds
"that Voyager article" and nothing else; it cannot find "the one about the
submarine robot" when the title says "autonomous bathyscaphe". A vector search
finds the bathyscaphe and also drags in six near-neighbours you did not mean.
Running both and merging keeps each one's strength without inheriting the
other's weakness.

**The merge rule, stated once so it is one policy rather than a heuristic per
call site: literal hits rank above semantic hits, then semantic by ascending
cosine distance, deduplicated by post id.** A literal title match is an
unambiguous signal about what the reader typed; a cosine neighbour is a guess,
and a guess never outranks a certainty.

The semantic leg rides `Story.centroid`. That vector already means "what this
post is about" and is one join away, so this needs no `Post.embedding` column
and no backfill — the canonical minimum, derived rather than copied.

Scope is published ARTICLE posts. An aggregate card has no title, summary or
body of its own (0011: it renders from its story's items at read time), so it
has nothing to match literally and nothing to show in a result row.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm import gateway
from ..llm.gateway import LLMError
from ..models import Post, Story
from .blocks import escape_like

log = logging.getLogger("episteme.search")


def merge_ids(literal: Sequence[int], semantic: Sequence[int], limit: int) -> list[int]:
    """The merge rule above, as a pure function over ids.

    Both inputs arrive already ordered by their own leg's notion of best-first,
    so merging is concatenation plus deduplication: the first appearance of an
    id fixes its rank, and a post found by both keeps its (higher) literal one.
    """
    seen: set[int] = set()
    merged: list[int] = []
    for post_id in (*literal, *semantic):
        if post_id in seen:
            continue
        seen.add(post_id)
        merged.append(post_id)
        if len(merged) >= limit:
            break
    return merged


def literal_query(query: str, limit: int):
    """Posts whose own title or summary contains `query` as a literal substring.

    `escape_like` is not optional: this is reader-typed free text, and a bare `%`
    would match every post while `_` would quietly over-match. Same reasoning,
    same helper, as the blocked-keyword patterns it was written for."""
    pattern = f"%{escape_like(query)}%"
    return (
        select(Post.id)
        .where(
            Post.status == "published",
            Post.kind == "article",
            or_(
                Post.title.ilike(pattern, escape="\\"),
                Post.summary.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(Post.generated_at.desc())
        .limit(limit)
    )


def semantic_query(vector: Sequence[float], limit: int):
    """Published articles nearest `vector`, by cosine distance over the centroid
    of the story each post was written from."""
    distance = Story.centroid.cosine_distance(vector)
    return (
        select(Post.id)
        .join(Story, Post.story_id == Story.id)
        .where(
            Post.status == "published",
            Post.kind == "article",
            Story.centroid.is_not(None),
        )
        .order_by(distance)
        .limit(limit)
    )


async def search_posts(session: AsyncSession, query: str, *, limit: int = 10) -> list[Post]:
    """Search published article posts, best match first.

    Degrades to literal-only when the embed endpoint is down, rather than
    failing: half a search is useful and an error is not, and `LLMError` is the
    contract every other caller degrades against (0003).
    """
    query = (query or "").strip()
    if not query:
        return []

    literal = list((await session.execute(literal_query(query, limit))).scalars())

    semantic: list[int] = []
    try:
        vector = (await gateway.embed([query]))[0]
    except LLMError as exc:
        log.warning("semantic search unavailable, falling back to literal only: %s", exc)
    else:
        semantic = list((await session.execute(semantic_query(vector, limit))).scalars())

    ranked = merge_ids(literal, semantic, limit)
    if not ranked:
        return []
    # One round trip for the rows, then re-imposed into rank order: SQL returns a
    # set, and `IN` says nothing about ordering.
    posts = (await session.execute(select(Post).where(Post.id.in_(ranked)))).scalars().all()
    by_id = {post.id: post for post in posts}
    return [by_id[post_id] for post_id in ranked if post_id in by_id]
