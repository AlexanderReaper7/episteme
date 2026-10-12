"""Fetching and extracting one article page, shared by every adapter whose items
link to articles (`rss`, `listing`)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import trafilatura
from trafilatura import extract_metadata

from ..models import Source
from .base import ExtractedItem
from .http import HttpMode, normalize_mode, polite_get


def source_mode(source: Source) -> HttpMode:
    """The source's HTTP transport mode (see ingest.http)."""
    return normalize_mode(source.config.get("http_mode"))


def source_host_gap(source: Source) -> float:
    """Extra per-host request spacing for touchy rate limiters (0 = global only)."""
    try:
        return float(source.config.get("min_request_gap_seconds", 0))
    except (TypeError, ValueError):
        return 0.0


# Actual video *embeds* only (iframe players), not mere links — a page's sidebar
# and comments are full of YouTube links, but an embed means the article itself
# presents the video. These populate kind="video" media_refs, the closed set the
# writer's video sections draw from.
_VIDEO_EMBED_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:youtube(?:-nocookie)?\.com/embed/[\w-]{6,}|player\.vimeo\.com/video/\d+)"
)


def find_video_embeds(html: str, limit: int = 3) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _VIDEO_EMBED_RE.finditer(html):
        url = match.group(0)
        if url in seen:
            continue
        seen.add(url)
        refs.append({"kind": "video", "url": url})
        if len(refs) >= limit:
            break
    return refs


_PUBLISHED_RE = re.compile(
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)'
)


def page_published(html: str) -> datetime | None:
    """`article:published_time`, the one date meta tag that carries a time of day.
    trafilatura's own date is a day, which would order a day's items arbitrarily."""
    match = _PUBLISHED_RE.search(html)
    if match is None:
        return None
    try:
        published = datetime.fromisoformat(match.group(1))
    except ValueError:
        return None
    return published if published.tzinfo else published.replace(tzinfo=UTC)


def page_title(title: str | None, sitename: str | None) -> str | None:
    """The page's title without the trailing site name most sites append to it
    ("... - VideoCardz.com")."""
    if not title or not sitename:
        return title
    for separator in (" - ", " | ", " – ", " — "):
        suffix = separator + sitename
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title


async def extract_page(url: str, source: Source) -> ExtractedItem:
    response = await polite_get(
        url, mode=source_mode(source), host_gap_seconds=source_host_gap(source)
    )
    response.raise_for_status()
    # favor_recall keeps more of the body ("prefer more text even when unsure")
    # so short-but-real articles aren't reduced to a caption. Outbound links
    # (e.g. a "Read more" to a richer source) live outside the main content and
    # are NOT recoverable here — the research agent parses those from raw HTML
    # via research.tools.fetch_page instead.
    text = trafilatura.extract(response.text, include_comments=False, favor_recall=True)
    media_refs = []
    title = None
    try:
        metadata = extract_metadata(response.text)
        if metadata is not None:
            if metadata.image:
                media_refs.append({"kind": "image", "url": metadata.image})
            title = page_title(metadata.title, metadata.sitename)
    except Exception:  # metadata is best-effort; never fail extraction over it
        pass
    media_refs.extend(find_video_embeds(response.text))
    return ExtractedItem(
        text=text,
        media_refs=media_refs,
        title=title,
        published_at=page_published(response.text),
    )
