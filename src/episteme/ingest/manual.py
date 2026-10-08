"""One URL the reader typed, turned into a story the writer will pick up.

Nothing in the pipeline could previously start from a URL. `SourceItem` requires
a `source_id`, and `pipeline.cluster_items` is the only thing that constructs a
`Story` — so "write me an article about this page" had no entry point at all.
This module is that entry point, and it is deliberately thin: it produces exactly
the rows the normal path produces, then gets out of the way. The article is
written by the same agentic writer, reviewed by the same QA stage, and ranked by
the same feed. A second, parallel "user article" pipeline is the thing this
avoids.

**Why a registered adapter for a source that fetches nothing.** Every source row
names an adapter (0002), and `get_adapter` raises for an unknown `type_name` — so
a `manual` source with no adapter would turn any code that walks all sources into
a landmine. Registering an adapter that yields nothing keeps the invariant true
at the cost of eight lines. Its `Source` row is `enabled=False`, so `ingest_all`
never asks it for anything anyway.

**Politeness (0005, 0006) is not weakened here.** `research.fetch_page` reaches
the network through `_pinned_get` -> `ingest.http.polite_get`, the same throttle
and the same honest User-Agent every source fetch uses, plus an SSRF guard the
source path does not have. What a typed URL genuinely skips is the *manual*
robots.txt check we do before adding a feed — which is a policy step performed by
a human, applied to a source we then poll forever, not to one page fetched once
because the reader asked for it by name.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm import gateway
from ..llm.gateway import LLMError
from ..models import Source, SourceItem, Story
from .base import ExtractedItem, RawItem, content_hash
from .registry import register

log = logging.getLogger("episteme.ingest.manual")

MANUAL_SOURCE_NAME = "Reader request"

# Enough text to be worth writing about at all. Below this the page is a paywall
# stub, a cookie wall, or a 404 that returned 200, and the honest answer is to
# say so now rather than spend a main-model hour discovering it.
MIN_USEFUL_CHARS = 400


@register
class ManualAdapter:
    """The adapter for URLs the reader hands us directly.

    It has nothing to poll: a manual source is a *container* for one-off items,
    not a feed. `fetch` returning an empty list is the whole implementation, and
    it means `ingest_all` picking this source up (it cannot — the row is
    disabled) would still be a no-op rather than an error.
    """

    type_name = "manual"

    async def fetch(self, source: Source, since: datetime | None) -> list[RawItem]:
        return []

    async def extract(self, item: RawItem, source: Source) -> ExtractedItem:
        # Extraction already happened in `ingest_url`, at the moment the reader
        # asked; there is no listing to come back to and re-extract from.
        return ExtractedItem(text=None)


async def manual_source(session: AsyncSession) -> Source:
    """The single `manual` source row, created on first use.

    Not in `seeds.py` because seeding only fires into an empty `sources` table —
    every existing database would never get one. Get-or-create is the mechanism
    that works on both a fresh install and a year-old one.
    """
    source = (
        (await session.execute(select(Source).where(Source.type_name == "manual")))
        .scalars()
        .first()
    )
    if source is not None:
        return source
    source = Source(
        type_name="manual",
        name=MANUAL_SOURCE_NAME,
        config={},
        # Never polled: it has no feed, and `ingest_all` skips disabled sources.
        enabled=False,
        # The reader chose it, so it is as credible as the reader thinks it is;
        # nothing here should quietly down-weight a page they asked for.
        credibility_rating=0.5,
    )
    session.add(source)
    await session.flush()
    log.info("Created the %r source (id=%d)", MANUAL_SOURCE_NAME, source.id)
    return source


class ManualIngestError(Exception):
    """The URL could not become a story. Carries a sentence for the reader."""


async def ingest_url(session: AsyncSession, url: str) -> Story:
    """Fetch `url` and produce a single-item story, pre-triaged for writing.

    The story is created already `triaged` with `triage_decision="write"`: triage
    exists to decide whether a cluster deserves a main-model hour, and the reader
    has already decided that. `origin="user"` is what the write stage reads to
    withhold `demote_story` and skip the thin-gate.

    Raises `ManualIngestError` with something worth showing a human. Callers are
    interactive, so a failure has to be a sentence, not a traceback.
    """
    # Imported here, not at module scope: `research` reaches the network through
    # `ingest.http`, so importing it from inside the `ingest` package's own
    # __init__ chain is a cycle. Same treatment `web.api` gives `worker.app`.
    from ..research import ResearchError, fetch_page

    try:
        page = await fetch_page(url)
    except ResearchError as exc:
        raise ManualIngestError(f"Could not read that page: {exc}") from exc

    text = (page.get("text") or "").strip()
    if len(text) < MIN_USEFUL_CHARS:
        raise ManualIngestError(
            f"That page gave only {len(text)} characters of article text "
            "(paywall, cookie wall, or a page that is mostly navigation). "
            "Try a direct link to the article itself."
        )

    # The FINAL post-redirect URL is the identity: a shortener and its target are
    # the same page, and `fetch_page` already resolved which one served this.
    final_url = page.get("url") or url
    item_hash = content_hash(final_url)
    existing = (
        (await session.execute(select(SourceItem).where(SourceItem.hash == item_hash)))
        .scalars()
        .first()
    )
    if existing is not None and existing.story_id is not None:
        raise ManualIngestError(
            f"Already have that page, as story {existing.story_id}. "
            "Ask me to rewrite it if the article needs redoing."
        )

    source = await manual_source(session)
    now = datetime.now(UTC)
    item = existing or SourceItem(source_id=source.id, url=final_url, hash=item_hash)
    item.title = page.get("title") or final_url
    item.raw_content = None
    item.extracted_text = text
    item.published_at = item.published_at or now
    item.fetch_status = "extracted"
    session.add(item)

    # Best effort. A missing vector costs this story its place in semantic search
    # and its affinity term in the feed; it does not stop the article being
    # written, and an interactive request must not fail because the embed server
    # is down. The `embed` stage heals the ITEM on its next pass; the story
    # centroid is set here or not at all, because nothing recomputes it for a
    # cluster that never grows.
    vector = None
    try:
        vector = (await gateway.embed([f"{item.title}\n\n{text}"]))[0]
    except LLMError as exc:
        log.warning("No embedding for reader-requested %s: %s", final_url, exc)
    else:
        item.embedding = vector

    story = Story(
        origin="user",
        status="triaged",
        triage_decision="write",
        triage_reason="Requested by the reader.",
        # Left NULL deliberately: triage never judged this story, and a made-up
        # quality score would be a lie the write queue reads as fact. Ordering is
        # handled by origin instead (pipeline._user_first).
        rank_score=None,
        topics=[],
        centroid=vector,
        item_count=1,
        first_item_at=now,
        last_item_at=now,
    )
    session.add(story)
    await session.flush()
    item.story_id = story.id
    await session.flush()
    log.info("Reader-requested story %d from %s", story.id, final_url)
    return story
