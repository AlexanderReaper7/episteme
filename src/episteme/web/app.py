from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import joinedload, selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Post, SourceItem, Story
from .admin import router as admin_router
from .api import router as api_router
from .templating import BASE_DIR, templates

app = FastAPI(title="Episteme")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(api_router)
app.include_router(admin_router)


async def _stream_page(session, page: int) -> dict:
    """Aggregation stream: Google-News-style cluster cards (spec §8, tier 2)."""
    size = settings.feed_page_size
    stories = (
        (
            await session.execute(
                select(Story)
                .options(selectinload(Story.items).joinedload(SourceItem.source))
                .where(Story.status == "aggregated")
                .order_by(Story.last_item_at.desc().nulls_last(), Story.id.desc())
                .offset((page - 1) * size)
                .limit(size + 1)
            )
        )
        .scalars()
        .all()
    )
    return {"stories": stories[:size], "page": page, "has_more": len(stories) > size}


async def _items_page(session, page: int) -> dict:
    """Raw source items — pre-LLM fallback stream (Phase 1 behavior)."""
    size = settings.feed_page_size
    items = (
        (
            await session.execute(
                select(SourceItem)
                .options(joinedload(SourceItem.source))
                .order_by(SourceItem.published_at.desc().nulls_last(), SourceItem.id.desc())
                .offset((page - 1) * size)
                .limit(size + 1)
            )
        )
        .scalars()
        .all()
    )
    return {"items": items[:size], "page": page, "has_more": len(items) > size}


@app.get("/", response_class=HTMLResponse)
async def feed(request: Request):
    async with SessionLocal() as session:
        posts = (
            (
                await session.execute(
                    select(Post)
                    .options(selectinload(Post.story).selectinload(Story.items))
                    .where(Post.status == "published")
                    .order_by(Post.generated_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        stream = await _stream_page(session, page=1)
        # Before the pipeline has produced anything, fall back to raw items so
        # the feed is useful from day one.
        fallback = None
        if not posts and not stream["stories"]:
            fallback = await _items_page(session, page=1)
    return templates.TemplateResponse(
        request,
        "feed.html",
        {"posts": posts, "stream": stream, "fallback": fallback},
    )


@app.get("/post/{post_id}", response_class=HTMLResponse)
async def post_view(request: Request, post_id: int):
    async with SessionLocal() as session:
        post = (
            await session.execute(
                select(Post)
                .options(selectinload(Post.story).selectinload(Story.items))
                .where(Post.id == post_id)
            )
        ).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "post.html", {"post": post})


@app.get("/partials/stream", response_class=HTMLResponse)
async def stream_partial(request: Request, page: int = Query(1, ge=1)):
    async with SessionLocal() as session:
        context = await _stream_page(session, page=page)
    return templates.TemplateResponse(request, "_stories.html", context)


@app.get("/partials/items", response_class=HTMLResponse)
async def items_partial(request: Request, page: int = Query(1, ge=1)):
    async with SessionLocal() as session:
        context = await _items_page(session, page=page)
    return templates.TemplateResponse(request, "_items.html", context)


@app.get("/health")
async def health():
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}
