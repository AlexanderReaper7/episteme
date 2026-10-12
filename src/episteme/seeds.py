"""Seed sources and voices — inserted once when their tables are empty (see
bootstrap.py). Afterwards, both are managed as rows in the database."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Source, Voice

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
    # Hardware and engineering (0063). Phoronix and OpenAI serve their feeds to an
    # honest client but 403 its article fetches, and extraction does not escalate;
    # robots.txt permits both paths, so they start impersonating. Verified 2026-10-12.
    ("TechPowerUp", "https://www.techpowerup.com/rss/news"),
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
    ("Chips and Cheese", "https://chipsandcheese.com/feed/"),
    ("ServeTheHome", "https://www.servethehome.com/feed/"),
    ("SweClockers", "https://www.sweclockers.com/feeds/nyheter", {"language": "sv"}),
    ("Phoronix", "https://www.phoronix.com/rss.php", {"http_mode": "impersonate"}),
    ("LWN", "https://lwn.net/headlines/rss"),
    ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss"),
    ("Ars Technica Gadgets", "https://feeds.arstechnica.com/arstechnica/gadgets"),
    # AI models, frontier and open-weight
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Mistral AI", "https://mistral.ai/rss.xml"),
    ("OpenAI", "https://openai.com/news/rss.xml", {"http_mode": "impersonate"}),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("Interconnects", "https://www.interconnects.ai/feed"),
    ("Ahead of AI", "https://magazine.sebastianraschka.com/feed"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
    ("Import AI", "https://importai.substack.com/feed"),
    # Swedish news: section feeds, because SVT's main feed is mostly every
    # region's local news and sport
    ("SVT Inrikes", "https://www.svt.se/nyheter/inrikes/rss.xml", {"language": "sv"}),
    ("SVT Ekonomi", "https://www.svt.se/nyheter/ekonomi/rss.xml", {"language": "sv"}),
    ("SVT Vetenskap", "https://www.svt.se/nyheter/vetenskap/rss.xml", {"language": "sv"}),
    ("SVT Väst", "https://www.svt.se/nyheter/lokalt/vast/rss.xml", {"language": "sv"}),
    ("forskning.se", "https://www.forskning.se/feed/", {"language": "sv"}),
    # Akamai 403s an honest client on every article; robots.txt permits /api/rss/
    # and /artikel/. The article page renders its text in script, and the feed
    # summary carries all of it, which `pipeline._plain_text` then prefers.
    (
        "Sveriges Radio Ekot",
        "https://api.sr.se/api/rss/program/83",
        {"http_mode": "impersonate", "language": "sv"},
    ),
]


#: Sources with no usable feed, read through the `listing` adapter (0063).
#: VideoCardz's Cloudflare 403s its feeds and sitemaps to every client, a browser
#: included, while curl_cffi gets the front page and the articles.
SEED_LISTINGS: list[tuple[str, dict]] = [
    (
        "VideoCardz",
        {
            "list_url": "https://videocardz.com/",
            "link_pattern": r"https://videocardz\.com/newz/[^/?#]+/?",
            "http_mode": "impersonate",
        },
    ),
]


async def seed_sources(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count(Source.id)))).scalar_one()
    if count > 0:
        return 0
    for name, feed_url, *rest in SEED_FEEDS:
        config = {"feed_url": feed_url, **(rest[0] if rest else {})}
        session.add(Source(type_name="rss", name=name, config=config))
    for name, config in SEED_LISTINGS:
        session.add(Source(type_name="listing", name=name, config=config))
    await session.commit()
    return len(SEED_FEEDS) + len(SEED_LISTINGS)


# The starting narration catalog: Fish reference-ids the user selected. `params`
# is the provider-agnostic generation bag (Fish reads temperature/top_p/prosody).
# sort_order 0 = the default voice. Add or edit voices as DB rows afterwards.
SEED_VOICES: list[dict] = [
    {
        "id": "c39a76f685cf4f8fb41cd5d3d66b497d",
        "label": "David Attenborough",
        "provider": "fish",
        "sort_order": 0,
        # A touch slower and a hair more consistent — measured narrator cadence.
        "params": {"temperature": 0.7, "top_p": 0.7, "prosody": {"speed": 0.95, "volume": 0}},
    },
    {
        "id": "ca3007f96ae7499ab87d27ea3599956a",
        "label": "Girl",
        "provider": "fish",
        "sort_order": 1,
        "params": {"temperature": 0.7, "top_p": 0.7, "prosody": {"speed": 1.0, "volume": 0}},
    },
]


async def seed_voices(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count(Voice.id)))).scalar_one()
    if count > 0:
        return 0
    for voice in SEED_VOICES:
        session.add(Voice(**voice))
    await session.commit()
    return len(SEED_VOICES)
