"""Shared Jinja2 environment + filters for all HTML routes (feed and admin)."""

import re
from datetime import datetime
from pathlib import Path

import markdown as md
import nh3
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


# Whitelist of embeddable players. Video section URLs are already closed-set
# (pipeline.sanitize_media_sections), but the iframe src is defense-in-depth:
# only these hosts ever become an embed; anything else renders as a plain link.
_EMBED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"https?://(?:www\.)?(youtube(?:-nocookie)?\.com)/embed/([\w-]{6,})"),
     r"https://www.youtube-nocookie.com/embed/\2"),
    (re.compile(r"https?://(?:www\.)?youtube\.com/watch\?(?:.*&)?v=([\w-]{6,})"),
     r"https://www.youtube-nocookie.com/embed/\1"),
    (re.compile(r"https?://youtu\.be/([\w-]{6,})"),
     r"https://www.youtube-nocookie.com/embed/\1"),
    (re.compile(r"https?://player\.vimeo\.com/video/(\d+)"),
     r"https://player.vimeo.com/video/\1"),
    (re.compile(r"https?://(?:www\.)?vimeo\.com/(\d+)"),
     r"https://player.vimeo.com/video/\1"),
]


def _video_embed(url: str | None) -> str | None:
    for pattern, replacement in _EMBED_PATTERNS:
        match = pattern.match(url or "")
        if match:
            return match.expand(replacement)
    return None


def _markdown(value: str) -> Markup:
    # python-markdown passes raw HTML through untouched, and the LLM prose it renders
    # is downstream of fetched web content (prompt-injectable) — so sanitize before
    # marking the result safe for the template.
    return Markup(nh3.clean(md.markdown(value)))


templates.env.filters["dt"] = _format_dt
templates.env.filters["banner_image"] = _banner_image
templates.env.filters["story_banner"] = _story_banner
templates.env.filters["markdown"] = _markdown
templates.env.filters["video_embed"] = _video_embed
