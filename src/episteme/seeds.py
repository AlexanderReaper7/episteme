"""Seed sources — inserted once when the sources table is empty (see bootstrap.py).
Afterwards, sources are managed as rows in the database."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Source

SEED_FEEDS: list[tuple[str, str] | tuple[str, str, dict]] = [
    # Science
    ("Nature", "https://www.nature.com/nature.rss"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
    # Phys.org's bot-detector fingerprints the TLS handshake: every honest httpx
    # request 429s regardless of User-Agent, but curl_cffi with a Chrome TLS
    # fingerprint gets the feed (its robots.txt permits /rss-feed/). `http_mode`
    # impersonate selects that transport (see ingest.http). Verified 2026-07-17.
    ("Phys.org", "https://phys.org/rss-feed/", {"http_mode": "impersonate"}),
    ("Ars Technica Science", "https://feeds.arstechnica.com/arstechnica/science"),
    ("Scientific American", "https://www.scientificamerican.com/platform/syndication/rss/"),
    ("Quanta Magazine", "https://www.quantamagazine.org/feed/"),
    # Space
    ("NASA Breaking News", "https://www.nasa.gov/rss/dyn/breaking_news.rss"),
    ("Space.com", "https://www.space.com/feeds/all"),
    ("Sky & Telescope", "https://www.skyandtelescope.org/feed/"),
    # CS / AI
    ("Ars Technica Tech", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
    # Archaeology / history
    ("Archaeology Magazine", "https://www.archaeology.org/feed"),
]


async def seed_sources(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count(Source.id)))).scalar_one()
    if count > 0:
        return 0
    for name, feed_url, *rest in SEED_FEEDS:
        config = {"feed_url": feed_url, **(rest[0] if rest else {})}
        session.add(Source(type_name="rss", name=name, config=config))
    await session.commit()
    return len(SEED_FEEDS)
