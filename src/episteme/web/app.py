from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import joinedload

from ..config import settings
from ..db import SessionLocal
from ..models import SourceItem

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


templates.env.filters["dt"] = _format_dt
templates.env.filters["banner_image"] = _banner_image


async def _feed_page(page: int) -> dict:
    size = settings.feed_page_size
    async with SessionLocal() as session:
        items = (
            (
                await session.execute(
                    select(SourceItem)
                    .options(joinedload(SourceItem.source))
                    .order_by(
                        SourceItem.published_at.desc().nulls_last(),
                        SourceItem.id.desc(),
                    )
                    .offset((page - 1) * size)
                    .limit(size + 1)  # one extra to detect another page
                )
            )
            .scalars()
            .all()
        )
    return {"items": items[:size], "page": page, "has_more": len(items) > size}


@app.get("/", response_class=HTMLResponse)
async def feed(request: Request):
    context = await _feed_page(page=1)
    return templates.TemplateResponse(request, "feed.html", context)


@app.get("/partials/items", response_class=HTMLResponse)
async def feed_items(request: Request, page: int = Query(1, ge=1)):
    context = await _feed_page(page=page)
    return templates.TemplateResponse(request, "_items.html", context)


@app.get("/health")
async def health():
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}
