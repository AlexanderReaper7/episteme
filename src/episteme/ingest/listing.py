"""Sources with no feed: a page that lists the articles, read for its links.

For a publisher that closes its feed to every client but serves its front page
and its articles (VideoCardz, 0063). Config:

- `list_url`: the page to read;
- `link_pattern`: a regex an article's absolute URL must match in full.

The list carries no clean title and no date, so both come from the article page
(`ExtractedItem.title`, `published_at`). With no date at fetch time the `since`
bound cannot apply here: every link on the page is returned, and the ingest task
skips the ones already stored before fetching anything.
"""

from __future__ import annotations

import re
from datetime import datetime

from lxml import html as lxml_html

from ..models import Source
from .base import ExtractedItem, RawItem
from .http import polite_get
from .page import extract_page, source_host_gap, source_mode
from .registry import register


def parse_listing(page: str, base_url: str, link_pattern: str) -> list[RawItem]:
    """Every distinct link on the page matching `link_pattern`, in page order
    (pure function; unit-testable)."""
    document = lxml_html.fromstring(page)
    document.make_links_absolute(base_url)
    pattern = re.compile(link_pattern)
    seen: set[str] = set()
    items: list[RawItem] = []
    for anchor in document.iter("a"):
        href = anchor.get("href", "")
        if href in seen or not pattern.fullmatch(href):
            continue
        seen.add(href)
        items.append(RawItem(url=href))
    return items


@register
class ListingAdapter:
    type_name = "listing"

    async def fetch(self, source: Source, since: datetime | None) -> list[RawItem]:
        list_url = source.config["list_url"]
        response = await polite_get(
            list_url, mode=source_mode(source), host_gap_seconds=source_host_gap(source)
        )
        response.raise_for_status()
        return parse_listing(response.text, list_url, source.config["link_pattern"])

    async def extract(self, item: RawItem, source: Source) -> ExtractedItem:
        return await extract_page(item.url, source)
