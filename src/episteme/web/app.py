from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import joinedload, selectinload
from starlette.types import Scope
from starlette_compress import CompressMiddleware

from ..config import settings
from ..db import SessionLocal
from ..models import Post, SourceItem, Story
from ..tts import default_voice_id, list_voices
from .admin import _group_calls
from .admin import router as admin_router
from .api import api_post_llm_calls, api_story
from .api import router as api_router
from .templating import BASE_DIR, render, templates


class RevalidateStaticFiles(StaticFiles):
    """Serve our own static assets with `Cache-Control: no-cache`.

    `no-cache` does NOT mean "don't store" — it means "store, but revalidate
    with the server before every reuse". StaticFiles already sends ETag +
    Last-Modified and answers conditional GETs with a cheap 304, so the browser
    stays one round-trip from fresh and never serves stale CSS/JS after an edit.
    This header rides only on OUR
    responses; externally-hosted (CDN) assets carry their own headers and keep
    caching as their servers dictate.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Episteme")
# Negotiates zstd > brotli > gzip > identity per request's Accept-Encoding;
# htmx partials and JSON API responses are the main beneficiaries.
app.add_middleware(CompressMiddleware)
app.mount("/static", RevalidateStaticFiles(directory=BASE_DIR / "static"), name="static")
# Narration MP3s the narrate stage writes into settings.audio_dir (bind-mounted to
# the host in compose). check_dir=False so importing the app never fails when the
# directory is absent (dev/tests); StaticFiles answers Range requests, so <audio>
# seeking works.
app.mount("/media", StaticFiles(directory=settings.audio_dir, check_dir=False), name="media")
app.include_router(api_router)
app.include_router(admin_router)


def _feed_sort_at(post: Post) -> datetime:
    """Python mirror of the SQL `feed_at` expression, so a rendered row can
    produce the keyset cursor for the next page without re-querying."""
    if post.kind == "aggregate":
        return post.story.last_item_at or post.generated_at
    return post.generated_at


async def _feed_page(session, cursor: tuple[datetime, int] | None = None) -> dict:
    """Unified vertical stream (spec §8): one recency-ordered column of every
    published post — generated `feature` articles and identity-only `aggregate`
    cluster cards interleaved as equal units. Features sort by when they were
    written; aggregates by their story's latest item (a cluster keeps surfacing
    as new sources join it), falling back to mint time if the story has none.

    Paged by keyset, not OFFSET: the stream grows at the head (features get
    written, aggregates re-sort up as sources join), so an offset window would
    re-serve rows that shifted down between the initial render and a `revealed`
    fetch — visible duplicates. The cursor is the last rendered row's
    `(feed_at, id)`; we fetch strictly below it. Because `feed_at` only ever
    increases for a given row, a row can never cross back below the cursor, so
    keyset here never duplicates (an aggregate that jumps to the head after
    being shown simply isn't re-fetched)."""
    size = settings.feed_page_size
    feed_at = case(
        (Post.kind == "aggregate", func.coalesce(Story.last_item_at, Post.generated_at)),
        else_=Post.generated_at,
    )
    stmt = (
        select(Post)
        .options(
            selectinload(Post.story)
            .selectinload(Story.items)
            .joinedload(SourceItem.source)
        )
        .join(Post.story)
        .where(Post.status == "published")
        .order_by(feed_at.desc(), Post.id.desc())
        .limit(size + 1)
    )
    if cursor is not None:
        cur_at, cur_id = cursor
        stmt = stmt.where(
            or_(feed_at < cur_at, and_(feed_at == cur_at, Post.id < cur_id))
        )
    posts = (await session.execute(stmt)).scalars().all()
    has_more = len(posts) > size
    posts = posts[:size]
    next_cursor = None
    if has_more and posts:
        last = posts[-1]
        next_cursor = {"at": _feed_sort_at(last).isoformat(), "id": last.id}
    return {
        "posts": posts,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "first_page": cursor is None,
    }


async def _items_page(session, cursor: tuple[datetime | None, int] | None = None) -> dict:
    """Raw source items — pre-LLM fallback stream (Phase 1 behavior). Keyset-paged
    for the same reason as `_feed_page` (items are ingested continuously at the
    head). Ordering is `published_at DESC NULLS LAST, id DESC`, so the cursor
    carries a nullable timestamp: a null `at` means the cursor is already inside
    the trailing null-`published_at` region, ordered by id alone."""
    size = settings.feed_page_size
    stmt = (
        select(SourceItem)
        .options(joinedload(SourceItem.source))
        .order_by(SourceItem.published_at.desc().nulls_last(), SourceItem.id.desc())
        .limit(size + 1)
    )
    if cursor is not None:
        cur_at, cur_id = cursor
        if cur_at is not None:
            stmt = stmt.where(
                or_(
                    SourceItem.published_at < cur_at,
                    and_(SourceItem.published_at == cur_at, SourceItem.id < cur_id),
                    SourceItem.published_at.is_(None),
                )
            )
        else:
            stmt = stmt.where(
                and_(SourceItem.published_at.is_(None), SourceItem.id < cur_id)
            )
    items = (await session.execute(stmt)).scalars().all()
    has_more = len(items) > size
    items = items[:size]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = {
            "at": last.published_at.isoformat() if last.published_at else "",
            "id": last.id,
        }
    return {
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "first_page": cursor is None,
    }


@app.get("/", response_class=HTMLResponse)
async def feed(request: Request):
    async with SessionLocal() as session:
        page = await _feed_page(session)
        # Before the pipeline has produced anything, fall back to raw items so
        # the feed is useful from day one.
        fallback = None
        if not page["posts"]:
            fallback = await _items_page(session)
    return render(
        request,
        "feed.html",
        {"feed": page, "fallback": fallback},
    )


@app.get("/post/{post_id}", response_class=HTMLResponse)
async def post_view(request: Request, post_id: int):
    async with SessionLocal() as session:
        post = (
            await session.execute(
                select(Post)
                .options(
                    selectinload(Post.story)
                    .selectinload(Story.items)
                    .joinedload(SourceItem.source),
                )
                .where(Post.id == post_id)
            )
        ).scalar_one_or_none()
        if post is None:
            raise HTTPException(status_code=404)
        voices = await list_voices(session)
        default_voice = await default_voice_id(session, settings.tts_default_voice)
    return render(
        request,
        "post.html",
        {
            "post": post,
            "voices": voices,
            "default_voice": default_voice,
            "tts_configured": bool(settings.fish_api_key),
        },
    )


@app.get("/post/{post_id}/provenance", response_class=HTMLResponse)
async def post_provenance(request: Request, post_id: int):
    """Provenance of THIS post version: its stamped LLM calls plus the shared
    story-level ones. Other versions (archived or newer) link to their own page."""
    async with SessionLocal() as session:
        story_id = (
            await session.execute(select(Post.story_id).where(Post.id == post_id))
        ).scalar_one_or_none()
    if story_id is None:
        raise HTTPException(status_code=404)
    story = await api_story(story_id)
    post = next(p for p in story["posts"] if p["id"] == post_id)
    calls = await api_post_llm_calls(post_id, full=True)
    return render(
        request,
        "post_provenance.html",
        {
            "post": post,
            "story": story,
            "groups": _group_calls(calls),
            "totals": {
                "calls": len(calls),
                "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
                "completion_tokens": sum(c["completion_tokens"] or 0 for c in calls),
                "duration_ms": sum(c["duration_ms"] or 0 for c in calls),
            },
        },
    )


@app.get("/partials/feed", response_class=HTMLResponse)
async def feed_partial(
    request: Request,
    cursor_at: str | None = None,
    cursor_id: int | None = None,
):
    # feed_at is never null (generated_at is NOT NULL), so a feed cursor always
    # carries a timestamp; ignore a malformed cursor and serve the head.
    cursor = None
    if cursor_at and cursor_id is not None:
        cursor = (datetime.fromisoformat(cursor_at), cursor_id)
    async with SessionLocal() as session:
        context = await _feed_page(session, cursor=cursor)
    return templates.TemplateResponse(request, "_feed.html", context)


@app.get("/partials/items", response_class=HTMLResponse)
async def items_partial(
    request: Request,
    cursor_at: str | None = None,
    cursor_id: int | None = None,
):
    # An empty `cursor_at` with an id is the null-`published_at` region cursor.
    cursor = None
    if cursor_id is not None:
        at = datetime.fromisoformat(cursor_at) if cursor_at else None
        cursor = (at, cursor_id)
    async with SessionLocal() as session:
        context = await _items_page(session, cursor=cursor)
    return templates.TemplateResponse(request, "_items.html", context)


@app.get("/health")
async def health():
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}
