from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import feedparser
import trafilatura
from trafilatura import extract_metadata

from ..models import Source
from .base import ExtractedItem, RawItem
from .http import polite_client
from .registry import register

log = logging.getLogger("episteme.ingest.rss")


def _entry_published(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = entry.get(attr)
        if parsed:
            return datetime(*parsed[:6], tzinfo=UTC)
    return None


def _entry_media(entry: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for media in entry.get("media_content", []):
        if media.get("url"):
            refs.append({"kind": "image", "url": media["url"]})
    for enclosure in entry.get("enclosures", []):
        href = enclosure.get("href") or enclosure.get("url")
        if href and str(enclosure.get("type", "")).startswith("image/"):
            refs.append({"kind": "image", "url": href})
    return refs


def parse_feed(content: bytes, since: datetime | None = None) -> list[RawItem]:
    """Parse raw RSS/Atom bytes into RawItems (pure function; unit-testable)."""
    parsed = feedparser.parse(content)
    items: list[RawItem] = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link:
            continue
        published = _entry_published(entry)
        if since is not None and published is not None and published <= since:
            continue
        items.append(
            RawItem(
                url=link,
                title=entry.get("title"),
                author=entry.get("author"),
                published_at=published,
                summary=entry.get("summary"),
                media_refs=_entry_media(entry),
            )
        )
    return items


@register
class RssAdapter:
    """RSS/Atom feeds, with full-article extraction of each entry's link."""

    type_name = "rss"

    async def fetch(self, source: Source, since: datetime | None) -> list[RawItem]:
        feed_url = source.config["feed_url"]
        # Conditional GET: send the validators from the last response so an
        # unchanged feed costs the server a 304 and no body.
        conditional_headers = {}
        if source.http_etag:
            conditional_headers["If-None-Match"] = source.http_etag
        if source.http_last_modified:
            conditional_headers["If-Modified-Since"] = source.http_last_modified
        async with polite_client(conditional_headers) as client:
            response = await client.get(feed_url)
            if response.status_code == 304:
                log.info("Feed unchanged (304): %s", feed_url)
                return []
            response.raise_for_status()
        source.http_etag = response.headers.get("ETag")
        source.http_last_modified = response.headers.get("Last-Modified")
        return parse_feed(response.content, since)

    async def extract(self, item: RawItem) -> ExtractedItem:
        async with polite_client() as client:
            response = await client.get(item.url)
            response.raise_for_status()
        text = trafilatura.extract(response.text, include_comments=False)
        media_refs = []
        try:
            metadata = extract_metadata(response.text)
            if metadata is not None and metadata.image:
                media_refs.append({"kind": "image", "url": metadata.image})
        except Exception:  # metadata is best-effort; never fail extraction over it
            pass
        return ExtractedItem(text=text, media_refs=media_refs)
