"""Reader-facing feedback routes — the HTML/htmx half of the recommendation
system, mirroring the JSON endpoints in `api.py`.

Every mutation returns the fragment it changed, re-read from the database: the
controls never track their own state client-side, so what you see after a click
is what was actually recorded. The `/tune` page is where the profile itself is
visible and editable in the reader's own words.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..models import Post, Source, SourceItem, Story, Topic
from ..recommend import feedback as feedback_service
from ..recommend import profile as profile_service
from ..recommend import topics as topics_service
from .templating import render, templates

log = logging.getLogger("episteme.web.feedback")

router = APIRouter()

# Which surface the controls are rendered on. A `Literal` rather than a bool: the
# fragment replaces itself, so it has to say where it lives in its own URLs, and
# an unrecognized value must 422 rather than silently fall back to the fuller
# control set. That was the actual defect here — card buttons sent nothing, the
# route defaulted to the article set, and clicking Dislike on a feed card swapped
# in the hide-source button on a card that had deliberately rendered without it.
Variant = Literal["card", "article"]


def _topic_entries(labels: list[str], index: dict) -> list[dict]:
    """Label to read, slug to key by.

    Signals are recorded against the topic's permanent slug, so pairing the two
    here is what keeps a chip rendering as pressed after the topic has been
    renamed. One definition, shared by the single-post and whole-page paths, so
    the two can't pair them differently."""
    return [{"label": label, "slug": topics_service.slug_for(label, index)} for label in labels]


def _post_labels(post: Post, story: Story | None) -> list[str]:
    """A post's own topics, falling back to its story's. An aggregate card has no
    topics of its own — it renders entirely from the story."""
    return list(post.topics or []) or list((story.topics if story else None) or [])


async def post_context(session: AsyncSession, post_id: int, variant: Variant = "article") -> dict:
    """Everything `_feedback.html` needs for one post's control set: the signals
    already recorded, the topics that can be steered, and — on the article page —
    the outlets that can be hidden."""
    post = await session.get(Post, post_id)
    if post is None:
        raise HTTPException(404, f"Unknown post {post_id}")
    story = await session.get(Story, post.story_id)
    sources = (
        await session.execute(
            select(Source.id, Source.name)
            .join(SourceItem, SourceItem.source_id == Source.id)
            .where(SourceItem.story_id == post.story_id)
            .distinct()
            .order_by(Source.name)
        )
    ).all()
    signals = await feedback_service.for_post(session, post_id)
    index = await topics_service.slug_index(session)
    return {
        "post_id": post_id,
        "signals": signals["kinds"],
        "topic_signals": signals["topics"],
        "source_signals": signals["sources"],
        "post_topics": _topic_entries(_post_labels(post, story), index),
        "post_sources": [{"id": row.id, "name": row.name} for row in sources],
        "variant": variant,
    }


async def feed_context(session: AsyncSession, posts: list[Post]) -> dict[int, dict]:
    """`post_context`'s card half for a whole page at once: `{post_id: context}`.

    Cards carry topic chips too, so they need the same label→slug pairing the
    article page does — but resolving it per card would mean a `slug_index` per
    post. The vocabulary is read once for the page instead, and the signals come
    from the single query the feed already makes. Outlets are deliberately absent:
    hiding a whole publisher is destructive and stays on the article page.
    """
    signals = await feedback_service.signals_for_posts(session, [post.id for post in posts])
    index = await topics_service.slug_index(session) if posts else {}
    contexts = {}
    for post in posts:
        bucket = signals.get(post.id) or {}
        # Aggregates already have `.story` loaded for the card banner, so reading
        # topics off it here costs no extra query.
        story = post.story if post.kind == "aggregate" else None
        contexts[post.id] = {
            "post_id": post.id,
            "signals": bucket.get("kinds") or {},
            "topic_signals": bucket.get("topics") or {},
            "source_signals": {},
            "post_topics": _topic_entries(_post_labels(post, story), index),
            "post_sources": [],
            "variant": "card",
        }
    return contexts


def _controls(request: Request, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, "_feedback.html", {"fb": context})


@router.post("/feedback", response_class=HTMLResponse)
async def feedback_record(
    request: Request,
    kind: str,
    post_id: int | None = Query(None, ge=1),
    topic: str | None = None,
    source_id: int | None = Query(None, ge=1),
    variant: Variant = "article",
):
    async with SessionLocal() as session:
        try:
            await feedback_service.record(
                session, kind, post_id=post_id, topic=topic, source_id=source_id
            )
        except feedback_service.FeedbackError as exc:
            raise HTTPException(422, str(exc)) from None
        if post_id is None:
            return HTMLResponse("")
        context = await post_context(session, post_id, variant)
    return _controls(request, context)


@router.post("/feedback/{feedback_id}/undo", response_class=HTMLResponse)
async def feedback_undo(
    request: Request,
    feedback_id: int,
    post_id: int | None = Query(None, ge=1),
    variant: Variant = "article",
):
    async with SessionLocal() as session:
        try:
            await feedback_service.undo(session, feedback_id)
        except feedback_service.FeedbackError as exc:
            raise HTTPException(404, str(exc)) from None
        if post_id is None:
            # Undo from the /tune list: re-render the whole profile panel.
            return await _tune_panel(request)
        context = await post_context(session, post_id, variant)
    return _controls(request, context)


# --- The tuning page --------------------------------------------------------------


async def _tune_context(
    echo: str | None = None,
    error: str | None = None,
    topic_error: str | None = None,
    block_error: str | None = None,
    saved: int = 0,
) -> dict:
    async with SessionLocal() as session:
        state = await profile_service.load(session)
        events = await feedback_service.recent(session, limit=30)
        posts = {
            post.id: post
            for post in (
                await session.execute(
                    select(Post).where(Post.id.in_([e.post_id for e in events if e.post_id]))
                )
            ).scalars()
        }
        sources = {
            row.id: row.name
            for row in (await session.execute(select(Source.id, Source.name))).all()
        }
        # Labels only — the vocabulary is a datalist for the "set a topic" box,
        # and selecting whole Topic rows would drag a 1024-dim embedding per entry
        # across for a list of strings.
        vocabulary = list(
            (await session.execute(select(Topic.label).order_by(Topic.label))).scalars().all()
        )
        slug_labels = await topics_service.slug_labels(session)
    return {
        "profile": state,
        "events": events,
        "posts": posts,
        "sources": sources,
        "vocabulary": vocabulary,
        # Slug -> current spelling, for rendering the slug-keyed events in the
        # signal list as words the reader recognizes.
        "topic_labels": slug_labels,
        # Weights are keyed by a topic's permanent slug; the reader reads labels,
        # so each row carries both — the slider posts the slug, the panel shows the
        # vocabulary's current spelling (a slug whose entry has since been pruned
        # falls back to reading as its own words). Sorted by magnitude so the
        # strongest pull and the strongest push are both at the top.
        "topic_rows": [
            (slug, slug_labels.get(slug) or slug.replace("-", " "), weight)
            for slug, weight in sorted(state.topic_weights.items(), key=lambda kv: -abs(kv[1]))
        ],
        # Bars are drawn as a fraction of the clamp, so the scale a weight is
        # measured against is the same one that bounds it.
        "clamp": settings.profile_weight_clamp,
        # Outlets that can be blocked from here. Already-blocked ones are rendered
        # as chips instead, so the picker only ever offers what isn't blocked yet.
        "blockable_sources": [
            {"id": source_id, "name": name}
            for source_id, name in sorted(sources.items(), key=lambda kv: kv[1])
            if source_id not in set(state.blocked_sources)
        ],
        "echo": echo,
        "error": error,
        "topic_error": topic_error,
        "block_error": block_error,
        "saved": saved,
    }


async def _tune_panel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_tune_panel.html", await _tune_context())


@router.get("/tune", response_class=HTMLResponse)
async def tune(request: Request):
    return render(request, "tune.html", await _tune_context())


@router.post("/tune/statement", response_class=HTMLResponse)
async def tune_statement(request: Request):
    """The natural-language channel: the reader's own words, parsed into profile
    changes and echoed back for confirmation. A parse failure is shown in place —
    nothing is recorded, so nothing has to be undone."""
    form = await request.form()
    text = (form.get("text") or "").strip()
    echo = error = None
    async with SessionLocal() as session:
        try:
            _, _, echo = await feedback_service.record_nl(session, text)
        except feedback_service.FeedbackError as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request, "_tune_panel.html", await _tune_context(echo=echo, error=error)
    )


# Slider fields are named `w:<slug>` so one form carries the whole weight list
# without the server having to know the vocabulary up front.
_WEIGHT_PREFIX = "w:"


def _number(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise feedback_service.FeedbackError(f"“{raw}” is not a number") from None


def changed_weights(submitted: dict[str, str], dirty_field: str | None) -> dict[str, str]:
    """Which of the submitted sliders the reader actually moved.

    The browser posts the whole list, so this is the guard against an untouched
    slider minting a `set_topic` that pins a weight nobody touched — a real risk,
    because the sliders render at the stored weight rounded to their step, so
    "submitted" is never a faithful copy of the profile.

    `dirty` (from app.js) is the only answer: which sliders moved is knowable only
    on the client, which holds each one's rendered starting value. A server-side
    comparison against that same rendered value is not a fallback but a different,
    worse rule — it would record every slider whose stored weight has drifted past
    its own rounding since the page was rendered, which the reader never touched.
    So the slider list requires JavaScript, states so in the panel, and an absent
    `dirty` records nothing rather than guessing. The "set a topic by name" box
    below it works without JS and can reach any topic, including these.
    """
    names = {name for name in (dirty_field or "").split(",") if name}
    return {slug: raw for slug, raw in submitted.items() if slug in names}


@router.post("/tune/weights", response_class=HTMLResponse)
async def tune_weights(request: Request):
    """Commit a batch of hand-dragged topic weights.

    Direct manipulation, deliberately not live: dragging several sliders is ONE
    editing session, so nothing is recorded until Save. Each topic still becomes
    its own `set_topic` event (undo stays per-topic) but the batch is one
    transaction and one profile replay.

    Only the sliders the reader actually moved are recorded — see
    `changed_weights` for how that is decided and why it matters.
    """
    form = await request.form()
    submitted = {
        key[len(_WEIGHT_PREFIX) :]: str(value)
        for key, value in form.items()
        if key.startswith(_WEIGHT_PREFIX)
    }
    dirty_field = form.get("dirty")
    error = None
    saved = 0
    changed = changed_weights(submitted, None if dirty_field is None else str(dirty_field))
    async with SessionLocal() as session:
        try:
            weights = {slug: _number(raw) for slug, raw in changed.items()}
            events, _ = await feedback_service.record_topic_weights(session, weights)
            saved = len(events)
        except feedback_service.FeedbackError as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request,
        "_tune_panel.html",
        await _tune_context(topic_error=error, saved=saved),
    )


@router.post("/tune/topic", response_class=HTMLResponse)
async def tune_topic(request: Request, topic: str | None = None):
    """Set ONE topic weight by name — the "set a topic" box, which is also the only
    way to give a weight to a topic that has no slider yet.

    The topic may arrive in the query string or the form; the weight always comes
    from the form.
    """
    form = await request.form()
    label = (topic or form.get("topic") or "").strip()
    raw_weight = (form.get("weight") or "").strip()
    error = None
    saved = 0
    async with SessionLocal() as session:
        try:
            if not label:
                raise feedback_service.FeedbackError("Name a topic to set")
            if not raw_weight:
                raise feedback_service.FeedbackError("Type a weight for it")
            await feedback_service.record(
                session, "set_topic", topic=label, weight=_number(raw_weight)
            )
            saved = 1
        except feedback_service.FeedbackError as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request,
        "_tune_panel.html",
        await _tune_context(topic_error=error, saved=saved),
    )


@router.post("/tune/block", response_class=HTMLResponse)
async def tune_block(request: Request):
    """Add a hard block by hand. Keyword or outlet — one control each, so the
    request never has to guess which one was meant."""
    form = await request.form()
    keyword = (form.get("keyword") or "").strip()
    raw_source = (form.get("source_id") or "").strip()
    error = None
    async with SessionLocal() as session:
        try:
            if keyword:
                await feedback_service.record(session, "block_keyword", keyword=keyword)
            elif raw_source:
                await feedback_service.record(session, "hide_source", source_id=int(raw_source))
            else:
                raise feedback_service.FeedbackError("Name a word or pick an outlet")
        except feedback_service.FeedbackError as exc:
            error = str(exc)
        except ValueError:
            error = "That outlet id isn't a number"
    return templates.TemplateResponse(
        request, "_tune_panel.html", await _tune_context(block_error=error)
    )


@router.post("/tune/unblock", response_class=HTMLResponse)
async def tune_unblock(
    request: Request,
    keyword: str | None = None,
    source_id: int | None = Query(None, ge=1),
):
    """Remove a hard block. The two kinds of block come off differently, and it is
    not an inconsistency: a keyword can have been blocked by a natural-language
    statement, so it needs a counter-event (deleting the statement would also
    delete the topic weights it set), while a source block can only come from
    `hide_source` events, which are deleted outright so the hide buttons on cards
    stay truthful. Both are exact — the profile is replayed either way."""
    error = None
    async with SessionLocal() as session:
        try:
            if keyword:
                await feedback_service.record(session, "unblock_keyword", keyword=keyword)
            elif source_id is not None:
                await feedback_service.unblock_source(session, source_id)
            else:
                raise feedback_service.FeedbackError("Nothing to unblock")
        except feedback_service.FeedbackError as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request, "_tune_panel.html", await _tune_context(block_error=error)
    )


@router.post("/tune/rebuild", response_class=HTMLResponse)
async def tune_rebuild(request: Request):
    async with SessionLocal() as session:
        await profile_service.rebuild(session)
    return await _tune_panel(request)
