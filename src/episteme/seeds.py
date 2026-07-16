"""Seed sources — inserted once when the sources table is empty (see bootstrap.py).
Afterwards, sources are managed as rows in the database."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Source

SEED_FEEDS: list[tuple[str, str]] = [
    # Science
    ("Nature", "https://www.nature.com/nature.rss"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
    ("Phys.org", "https://phys.org/rss-feed/"),
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
    for name, feed_url in SEED_FEEDS:
        session.add(Source(type_name="rss", name=name, config={"feed_url": feed_url}))
    await session.commit()
    return len(SEED_FEEDS)
