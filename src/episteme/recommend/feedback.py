"""Recording reader signals — the write side of the profile.

Every function here does the same two things: append one row to the canonical
`feedback` log, then rebuild the derived profile from it. There is deliberately
no incremental "apply this event to the profile" path; that is what would let
the cache drift away from the log.

Recording is where the event's payload is captured (`Feedback` explains why):
what the reader reacted to is read once, here, and frozen onto the row.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..llm import LLMError, gateway
from ..llm.observe import llm_context
from ..llm.prompts import FEEDBACK_INTENT_SYSTEM
from ..llm.schemas import ProfileIntent
from ..models import Feedback, Post, SourceItem, Story
from . import profile as profile_module
from . import topics as topics_module
from .profile import ProfileState, rebuild
from .scoring import defer_rescore

log = logging.getLogger("episteme.recommend.feedback")

# What each signal means and what it needs. `post` signals speak about one post's
# content; `topic` and `source` signals are aimed at a dimension directly.
POST_KINDS = ("like", "dislike", "save")
TOPIC_KINDS = ("more_topic", "less_topic")
# The hand-edited weight from /tune. Kept out of TOPIC_KINDS on purpose: those two
# are the paired more/less buttons on a card, and the rendered button state reads
# that tuple. A set carries a number instead of a direction.
SET_KINDS = ("set_topic",)
SOURCE_KINDS = ("hide_source",)
# Hard keyword blocks, added and removed by hand on /tune. Unblocking is its own
# event rather than the deletion of whatever imposed the block, because a block can
# come from a natural-language refusal — see `profile.replay`.
KEYWORD_KINDS = ("block_keyword", "unblock_keyword")
KINDS = (
    *POST_KINDS,
    *TOPIC_KINDS,
    *SET_KINDS,
    *SOURCE_KINDS,
    *KEYWORD_KINDS,
    "nl_feedback",
)


class FeedbackError(ValueError):
    """A signal that cannot be recorded as asked (unknown kind, missing target)."""


async def _snapshot(session: AsyncSession, post_id: int) -> dict:
    """Freeze what the reader is reacting to. A feature post carries its own
    topics and difficulty; an aggregate card has neither — it renders from its
    story — so the story supplies them. The embedding is the story centroid,
    which is what the scorer compares candidates against.

    Topics are frozen as canonical SLUGS, not the labels the post displays: the
    slug is the topic's permanent identity, so a later rename cannot detach a
    signal from the weight it was supposed to feed."""
    post = await session.get(Post, post_id)
    if post is None:
        raise FeedbackError(f"Unknown post {post_id}")
    story = await session.get(Story, post.story_id)
    source_ids = list(
        (
            await session.execute(
                select(SourceItem.source_id)
                .where(SourceItem.story_id == post.story_id)
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    labels = list(post.topics or []) or list((story.topics if story else None) or [])
    index = await topics_module.slug_index(session)
    return {
        "post_id": post.id,
        "embedding": list(story.centroid) if story is not None and story.centroid is not None else None,
        "topics_snapshot": [topics_module.slug_for(label, index) for label in labels],
        "source_ids": source_ids,
        "difficulty": post.difficulty,
    }


def _clean_weight(kind: str, weight: float | None) -> float:
    """Validate and clamp a hand-set weight. Shared by the single-signal and batch
    paths so a slider and the API can never disagree about what is storable."""
    if weight is None:
        raise FeedbackError(f"{kind} needs a weight")
    try:
        value = float(weight)
    except (TypeError, ValueError):
        raise FeedbackError(f"{weight!r} is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise FeedbackError("A weight has to be a finite number")
    # Clamped where it is RECORDED, not only where it is replayed: the log is
    # canonical, so a value it could never produce shouldn't be stored in it.
    limit = settings.profile_weight_clamp
    return max(-limit, min(limit, value))


async def _topic_slug(session: AsyncSession, topic: str) -> str:
    """The vocabulary slug a reader-supplied topic names, extending the vocabulary
    if it names something new. Falls back to the raw text's own slug when
    resolution finds nothing at all, so a signal is never silently dropped —
    `profile.replay` slugifies its keys, so that still lands somewhere stable."""
    resolved = await topics_module.resolve_slugs(session, [topic])
    return resolved[0] if resolved else topics_module.slugify(topic)


async def record(
    session: AsyncSession,
    kind: str,
    *,
    post_id: int | None = None,
    topic: str | None = None,
    source_id: int | None = None,
    weight: float | None = None,
    keyword: str | None = None,
) -> tuple[Feedback, ProfileState]:
    """Append one explicit signal and rebuild the profile."""
    if kind not in KINDS or kind == "nl_feedback":
        raise FeedbackError(f"Unknown feedback kind {kind!r}; expected one of {KINDS[:-1]}")

    payload: dict = {"kind": kind}
    if kind in POST_KINDS:
        if post_id is None:
            raise FeedbackError(f"{kind} needs a post_id")
        payload.update(await _snapshot(session, post_id))
    elif kind in TOPIC_KINDS:
        if not topic:
            raise FeedbackError(f"{kind} needs a topic")
        # Resolved to a SLUG so "more of this" lands on the same weight key the
        # scorer reads, whatever spelling the card happened to show — and keeps
        # landing there after the topic is renamed.
        payload["topic"] = await _topic_slug(session, topic)
        payload["post_id"] = post_id
    elif kind in SET_KINDS:
        if not topic:
            raise FeedbackError(f"{kind} needs a topic")
        payload["value"] = _clean_weight(kind, weight)
        payload["topic"] = await _topic_slug(session, topic)
    elif kind in KEYWORD_KINDS:
        cleaned = profile_module.normalize_keyword(keyword)
        if not cleaned:
            raise FeedbackError(f"{kind} needs a keyword")
        payload["keyword"] = cleaned
    else:  # hide_source
        if source_id is None:
            raise FeedbackError("hide_source needs a source_id")
        payload["source_ids"] = [source_id]
        payload["post_id"] = post_id

    event = Feedback(**payload)
    session.add(event)
    await session.commit()
    state = await rebuild(session)
    await defer_rescore()
    return event, state


async def record_topic_weights(
    session: AsyncSession, weights: dict[str, float]
) -> tuple[list[Feedback], ProfileState]:
    """Set several topic weights at once — one editing session, one commit.

    Keyed by SLUG, already canonical: this is the /tune slider list, whose fields
    are named from the profile's own keys (`w:<slug>`), so there is nothing to
    resolve and nothing that could resolve differently on the way back in. The
    free-text "set a topic by name" box goes through `record` instead, which does
    resolve.

    Each topic still becomes its own `set_topic` event, so undo stays per-topic
    rather than all-or-nothing, but the log is appended in a single transaction and
    the profile is replayed ONCE at the end. That matters: `rebuild` is a full
    replay, and calling it per slider would replay the whole log N times to reach
    the state the last one produces anyway.

    Every value is validated before any of them is written, so a bad one in the
    batch fails the whole batch rather than leaving half of it applied — and, as
    with `record`, the malformed cases never reach the database at all.
    """
    if not weights:
        return [], await profile_module.load(session)

    cleaned: list[tuple[str, float]] = []
    for slug, weight in weights.items():
        if not slug:
            raise FeedbackError("set_topic needs a topic")
        cleaned.append((slug, _clean_weight("set_topic", weight)))

    events = [
        Feedback(kind="set_topic", topic=slug, value=value) for slug, value in cleaned
    ]
    session.add_all(events)
    await session.commit()
    state = await rebuild(session)
    await defer_rescore()
    return events, state


async def unblock_source(session: AsyncSession, source_id: int) -> ProfileState:
    """Remove a source's hard block by deleting the `hide_source` events behind it.

    Deliberately NOT the counter-event that `unblock_keyword` is, because the two
    blocks have different origins. A source block can only come from a
    `hide_source` event, so deleting those is an exact undo that leaves no residue
    — and it keeps the per-post controls honest: the hide button on a card renders
    as pressed when its event exists, so a counter-event would leave that button
    claiming the outlet is hidden after the reader unblocked it here. A keyword
    block has no single originating event to delete (a statement can impose one),
    which is why that direction needs an event of its own.
    """
    # Filtered in Python rather than with a JSONB containment predicate: the whole
    # set of hide_source events for one reader is a handful of rows, and `source_ids`
    # is a snapshot array, not an indexed relation.
    events = (
        (await session.execute(select(Feedback).where(Feedback.kind == "hide_source")))
        .scalars()
        .all()
    )
    for event in events:
        if source_id in (event.source_ids or []):
            await session.delete(event)
    await session.commit()
    state = await rebuild(session)
    await defer_rescore()
    return state


async def record_nl(session: AsyncSession, text: str) -> tuple[Feedback, ProfileState, str]:
    """Parse a free-text statement into profile changes, record both the words and
    the parse, and rebuild. Returns the echo sentence for the reader.

    The parse is stored because replay must never need a second LLM call to
    reinterpret the same sentence — and because the `llm_calls` row holding the
    verbatim prompt is pruned long before the statement stops mattering."""
    statement = (text or "").strip()
    if not statement:
        raise FeedbackError("Say something for the profile to learn from")

    vocabulary = await topics_module.vocabulary_for_prompt(session)
    prompt = f"Reader's statement:\n\n{statement}"
    if vocabulary:
        prompt += "\n\nExisting topics (reuse exact wording where one fits):\n"
        prompt += ", ".join(vocabulary)
    try:
        with llm_context(stage="feedback"):
            intent = await gateway.complete_json(
                "fast", FEEDBACK_INTENT_SYSTEM, prompt, ProfileIntent
            )
    except LLMError as exc:
        raise FeedbackError(f"Could not interpret that right now: {exc}") from exc

    # The reader's own words are authoritative, so a topic they raised may extend
    # the vocabulary — unlike a model-invented tag, which only ever gets folded in.
    #
    # Resolved through the per-label MAPPING, never by zipping against a list.
    # `resolve*` deduplicates, so its output is not positionally aligned with its
    # input, and pairing them by position silently swaps one entry's topic for
    # another's — "less crypto, more quantum computing" recorded as its own
    # inverse, into the `parsed_intent` that replay reads for as long as the
    # statement stands.
    if intent.topics:
        entries = await topics_module.resolve_entries(
            session, [entry.topic for entry in intent.topics]
        )
        for entry in intent.topics:
            resolved = entries.get(entry.topic)
            entry.topic = resolved.slug if resolved else topics_module.slugify(entry.topic)

    event = Feedback(
        kind="nl_feedback",
        nl_text=statement,
        parsed_intent=intent.model_dump(),
    )
    session.add(event)
    await session.commit()
    state = await rebuild(session)
    await defer_rescore()
    return event, state, intent.echo


async def undo(session: AsyncSession, feedback_id: int) -> ProfileState:
    """Remove one event and rebuild. Exact, not approximate: the profile after
    the undo is the profile that would have existed had the event never happened
    — which is the whole reason the log is canonical."""
    event = await session.get(Feedback, feedback_id)
    if event is None:
        raise FeedbackError(f"Unknown feedback {feedback_id}")
    await session.delete(event)
    await session.commit()
    state = await rebuild(session)
    await defer_rescore()
    return state


async def recent(session: AsyncSession, limit: int = 50) -> list[Feedback]:
    return list(
        (
            await session.execute(
                select(Feedback).order_by(Feedback.created_at.desc(), Feedback.id.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )


def _empty_signals() -> dict:
    return {"kinds": {}, "topics": {}, "sources": {}}


async def signals_for_posts(
    session: AsyncSession, post_ids: list[int]
) -> dict[int, dict]:
    """Which signals already exist on each of these posts, in ONE query for the
    whole page — the feed renders its buttons in the state the reader left them,
    and an active button carries its own event id so undo stays exact.

    Shape per post: `{"kinds": {kind: id}, "topics": {(slug, kind): id},
    "sources": {source_id: id}}`. Topic keys are canonical slugs, as stored on the
    events — the template pairs each of a post's labels with its slug so the
    button state survives a rename. Later events win, so re-clicking after an undo
    behaves as expected."""
    if not post_ids:
        return {}
    events = (
        await session.execute(
            select(Feedback)
            .where(Feedback.post_id.in_(post_ids), Feedback.kind != "nl_feedback")
            .order_by(Feedback.id)
        )
    ).scalars().all()
    signals: dict[int, dict] = {}
    for event in events:
        bucket = signals.setdefault(event.post_id, _empty_signals())
        if event.kind in POST_KINDS:
            bucket["kinds"][event.kind] = event.id
        elif event.kind in TOPIC_KINDS and event.topic:
            bucket["topics"][(event.topic, event.kind)] = event.id
        elif event.kind in SOURCE_KINDS:
            for source_id in event.source_ids or []:
                bucket["sources"][source_id] = event.id
    return signals


async def for_post(session: AsyncSession, post_id: int) -> dict:
    """`signals_for_posts` for a single post, with the empty shape as a default so
    templates never branch on absence."""
    return (await signals_for_posts(session, [post_id])).get(post_id) or _empty_signals()
