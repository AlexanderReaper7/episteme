from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..models import Source

# Query parameters that only track, never identify content.
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src")


def canonicalize_url(url: str) -> str:
    parts = urlparse(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PARAMS)
    ]
    return urlunparse(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.params,
            urlencode(query),
            "",  # drop fragment
        )
    )


def content_hash(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode()).hexdigest()


@dataclass
class RawItem:
    """A feed/listing entry as the source reported it, before full-text extraction."""

    url: str
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    media_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExtractedItem:
    """Result of following the item's link and extracting the full article."""

    text: str | None
    media_refs: list[dict[str, Any]] = field(default_factory=list)


class SourceAdapter(Protocol):
    """One implementation per source type; registered in ingest.registry."""

    type_name: str

    async def fetch(self, source: Source, since: datetime | None) -> list[RawItem]: ...

    async def extract(self, item: RawItem) -> ExtractedItem: ...
