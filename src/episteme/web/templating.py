"""Shared Jinja2 environment + filters for all HTML routes (feed and admin)."""

from datetime import datetime
from pathlib import Path

import markdown as md
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..models import Story

BASE_DIR = Path(__file__).parent

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
