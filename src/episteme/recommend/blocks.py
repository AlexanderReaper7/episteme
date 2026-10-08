"""Hard blocks, defined once.

Two places need to know what "blocked" excludes: the feed (don't show it) and the
write queue (don't spend main-model minutes writing it). Expressing that twice
would be two definitions that drift — a keyword the feed hides but the writer
still spends a GPU-hour on. So the predicate lives here and both callers pass in
whichever column identifies the story.

Blocks FILTER, unlike every other profile signal, which only reorders (spec §8:
"hard blocks filter first"). Nothing disappears because a post was disliked; a
blocked outlet or an explicit keyword refusal, on the other hand, means gone.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select

from ..models import SourceItem
from .profile import ProfileState


_LIKE_ESCAPE = "\\"


def escape_like(keyword: str) -> str:
    """Reader-typed text matched as a literal substring, so LIKE's own
    wildcards have to be neutralized before it becomes a pattern.

    Public because `recommend.search` needs exactly this for the same reason:
    two escapers is the shape that drifts until one of them stops escaping.

    Unescaped, `%` in a keyword matches everything — one block would empty the
    feed AND the write queue with no error anywhere — and `_` quietly
    over-matches ("ai_hype" hiding "ai hype", "aizhype", any single character).
    Keywords are reader-typed free text, including from natural-language
    statements, so neither character is hypothetical. The backslash goes first;
    escaping it after the wildcards would re-escape the escapes."""
    for char in (_LIKE_ESCAPE, "%", "_"):
        keyword = keyword.replace(char, _LIKE_ESCAPE + char)
    return keyword


def filters(profile: ProfileState, story_id_column, text_columns: tuple = ()) -> list:
    """SQLAlchemy conditions excluding blocked content.

    `story_id_column` is whatever identifies the story in the caller's query
    (`Post.story_id` from the feed, `Story.id` from the write queue).
    `text_columns` are additional columns to keyword-match — the feed passes a
    post's own title and summary, which the story's items don't contain.
    """
    conditions = []
    if profile.blocked_sources:
        # A story usually has several sources, so it survives while ANY
        # contributing source is unblocked: hiding one outlet must not silently
        # take the others' coverage of the same event with it.
        conditions.append(
            select(SourceItem.id)
            .where(
                SourceItem.story_id == story_id_column,
                SourceItem.source_id.not_in(profile.blocked_sources),
            )
            .exists()
        )
    for keyword in profile.blocked_keywords:
        pattern = f"%{escape_like(keyword)}%"
        # Match through the story's items, not only the caller's own text: an
        # aggregate card has no title or summary of its own (it renders from its
        # items), so a post-level-only match would miss most of the feed.
        matches = [
            select(SourceItem.id)
            .where(
                SourceItem.story_id == story_id_column,
                or_(
                    SourceItem.title.ilike(pattern, escape=_LIKE_ESCAPE),
                    SourceItem.extracted_text.ilike(pattern, escape=_LIKE_ESCAPE),
                ),
            )
            .exists()
        ]
        # COALESCE is load-bearing, not defensive. An aggregate card's own title
        # and summary are NULL, and `NULL ILIKE ...` is NULL, not false — so a
        # bare `~or_(exists, title.ilike(...), ...)` evaluates to NULL for every
        # aggregate and WHERE discards it. One blocked keyword would silently
        # empty the feed of every aggregate card in it.
        matches.extend(
            func.coalesce(column, "").ilike(pattern, escape=_LIKE_ESCAPE) for column in text_columns
        )
        conditions.append(~or_(*matches))
    return conditions
