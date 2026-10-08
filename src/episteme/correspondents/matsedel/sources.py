"""The four restaurants, as `sources` rows, and the adapter that owns their type.

**One row per restaurant, not one for Matsedel.** Each site then keeps its own
cooldown, its own HTTP cache validators and its own enabled flag, so the week one
of them redesigns its page it can be switched off in `/admin/sources` without
taking lunch down.

**Why an adapter for sources that ingestion never fetches.** Every source row
names an adapter (0002) and `get_adapter` raises for an unknown `type_name`, so
four `matsedel` rows with no adapter would turn anything that walks all sources
into a landmine. `ingest/manual.py` is the precedent. What keeps ingestion away
is not the enabled flag, which is the reader's switch here: `ingest_all` skips
every source whose `type_name` is a correspondent slug, so `enabled` keeps its
plain meaning and turning one restaurant off in `/admin/sources` is the week its
page was redesigned.

`type_name` is also the correspondent's slug, which `filing` checks rather than
trusts: a filed post's card names its correspondent by walking to this column
(0046).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...ingest.base import ExtractedItem, RawItem
from ...ingest.registry import register as register_adapter
from ...models import Source

log = logging.getLogger("episteme.correspondents.matsedel")

TYPE_NAME = "matsedel"

#: robots.txt checked by hand 2026-08-28, all four allow it. Squarespace
#: (restaurangitalia) lists `ClaudeBot`, `anthropic-ai` and `GPTBot` in the same
#: group as `*` with no `Disallow: /`, so its block-AI toggle is off; the others
#: are a WordPress default, a permissive default, and no robots.txt at all.
#: `site` is the stable identity inside a filed item's period key, so it must not
#: change once a week has been filed under it.
RESTAURANTS = (
    {"site": "koppargrillen", "name": "Koppargrillen", "url": "https://koppargrillen.se/lunch/"},
    {
        "site": "italia",
        "name": "Restaurang Italia",
        "url": "https://www.restaurangitalia.com/menu-1",
    },
    {
        "site": "vanerparken",
        "name": "Restaurang Vänerparken",
        "url": "https://www.vanerparken.com/matsedel/",
    },
    {"site": "kalasboden", "name": "Kalasboden", "url": "https://www.kalasboden.se/"},
)


@register_adapter
class MatsedelAdapter:
    """The adapter for a correspondent's own sources: it fetches nothing.

    Matsedel reads its sites through `readers.read_source`, on a schedule of its
    own, and files finished posts. There is no listing for ingestion to poll and
    nothing here for the pipeline to embed, cluster, triage or write.
    """

    type_name = TYPE_NAME

    async def fetch(self, source: Source, since: datetime | None) -> list[RawItem]:
        return []

    async def extract(self, item: RawItem, source: Source) -> ExtractedItem:
        return ExtractedItem(text=None)


async def ensure_sources(session: AsyncSession) -> int:
    """Get-or-create the four rows. Returns how many were added.

    Not a seed, for the reason `correspondents/rows.py` gives: seeding fires only
    into an empty table, so an existing database would never get a row for a
    correspondent installed later. Keyed on `config["site"]` rather than the name,
    because the name is what a card prints and may be corrected.
    """
    known = {
        (row.config or {}).get("site"): row
        for row in (
            await session.execute(select(Source).where(Source.type_name == TYPE_NAME))
        ).scalars()
    }
    added = 0
    for restaurant in RESTAURANTS:
        row = known.get(restaurant["site"])
        if row is None:
            session.add(
                Source(
                    type_name=TYPE_NAME,
                    name=restaurant["name"],
                    config={"site": restaurant["site"], "url": restaurant["url"]},
                )
            )
            added += 1
            log.info("Registered lunch source %r", restaurant["site"])
        elif not (row.config or {}).get("url"):
            row.config = {**(row.config or {}), "url": restaurant["url"]}
    return added


async def restaurants(session: AsyncSession) -> list[Source]:
    """The rows Matsedel reads, in a stable order. A disabled row is skipped, which
    is how one site is switched off in `/admin/sources` when its page changes."""
    return list(
        (
            await session.execute(
                select(Source)
                .where(Source.type_name == TYPE_NAME, Source.enabled.is_(True))
                .order_by(Source.id)
            )
        ).scalars()
    )
