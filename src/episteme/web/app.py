from datetime import datetime
from pathlib import Path

import markdown as md
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select, text
from sqlalchemy.orm import joinedload, selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Article, SourceItem, Story

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Episteme")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _format_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def _banner_image(media_refs: list | None) -> str | None:
    for ref in media_refs or []:
        if ref.get("kind") == "image" and ref.get("url"):
            return ref["url"]
    return None


def _story_banner(story: Story) -> str | None:
    for item in story.items:
        banner = _banner_image(item.media_refs)
        if banner:
            return banner
    return None


def _markdown(value: str) -> Markup:
    return Markup(md.markdown(value))


templates.env.filters["dt"] = _format_dt
templates.env.filters["banner_image"] = _banner_image
templates.env.filters["story_banner"] = _story_banner
templates.env.filters["markdown"] = _markdown


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
        articles = (
            (
                await session.execute(
                    select(Article)
                    .options(selectinload(Article.story).selectinload(Story.items))
                    .where(Article.status == "published")
                    .order_by(Article.generated_at.desc())
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
        if not articles and not stream["stories"]:
            fallback = await _items_page(session, page=1)
    return templates.TemplateResponse(
        request,
        "feed.html",
        {"articles": articles, "stream": stream, "fallback": fallback},
    )


@app.get("/article/{article_id}", response_class=HTMLResponse)
async def article_view(request: Request, article_id: int):
    async with SessionLocal() as session:
        article = (
            await session.execute(
                select(Article)
                .options(selectinload(Article.story).selectinload(Story.items))
                .where(Article.id == article_id)
            )
        ).scalar_one_or_none()
    if article is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "article.html", {"article": article})


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
