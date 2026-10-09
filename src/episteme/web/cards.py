"""What a feed card shows, decided once per kind (0061).

`_feed.html` renders a `Card`, `/api/feed` serializes one, and the feed's
validators hash them, so the web feed, the Android app and the ETag all read
the same decisions. Before this the template made them itself, per kind, and the
validator kept a hand-written copy of the fields it thought the template read.
That copy missed an aggregate's summary and href.

Only the decisions live here: where the card goes, which picture, which time,
whose name. How a kind LOOKS (its icon, the "Article" chip, the Episteme byline)
stays in each client.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from ..models import Post
from .templating import _story_banner


@dataclass(frozen=True)
class Card:
    post_id: int
    kind: str
    # One destination rule for every kind (0047): the post's own href, or its
    # page when it renders itself. `/post/{id}` redirects to the same place.
    destination: str
    title: str
    summary: str | None
    # Hotlinked, never cached (spec §6); a client drops it when it fails to load.
    banner: str | None
    # The time the card wears. An aggregate ages with its newest item, a filed
    # post from when it became visible, an article from when it was written.
    at: datetime | None
    topics: list[str]
    # The outlet behind an aggregate or filed card; None on an article.
    source_name: str | None
    # How many more items the story holds beyond the one the card names.
    more_items: int
    # Whose byline a filed card prints (0046); None on every other kind.
    correspondent: str | None
    # An article's own metadata; None on the kinds Episteme did not write.
    reading_time_minutes: int | None
    difficulty: str | None

    @property
    def offsite(self) -> bool:
        return self.destination.startswith("http")


def build(post: Post, correspondent_labels: dict[str, str]) -> Card | None:
    """The card for one post, or None for a post that has nothing to show: an
    aggregate whose story has lost every item has no title to print.

    Reads `post.story.items` for aggregate and filed posts, so the caller loads
    them (`web.app._load_story_items`)."""
    destination = post.href or f"/post/{post.id}"
    if post.kind == "aggregate":
        story = post.story
        primary = story.items[0] if story.items else None
        if primary is None:
            return None
        return Card(
            post_id=post.id,
            kind=post.kind,
            destination=destination,
            title=primary.title or primary.url,
            summary=post.summary,
            banner=_story_banner(story),
            at=story.last_item_at,
            topics=list(story.topics or []),
            source_name=primary.source.name,
            more_items=len(story.items) - 1,
            correspondent=None,
            reading_time_minutes=None,
            difficulty=None,
        )
    if post.kind == "filed":
        story = post.story
        primary = story.items[0] if story.items else None
        # `sources.type_name` IS the correspondent's slug (0046), and
        # `filing._require_correspondent_source` is what keeps that true. A slug
        # with no row falls back to the source's own name.
        slug = primary.source.type_name if primary else None
        return Card(
            post_id=post.id,
            kind=post.kind,
            destination=destination,
            title=post.title,
            summary=post.summary,
            banner=_story_banner(story),
            # When it became visible, not when the correspondent read it: one read
            # of a week mints five posts, and all five would otherwise wear Monday.
            at=post.publish_at or post.generated_at,
            topics=list(post.topics or []),
            source_name=primary.source.name if primary else None,
            more_items=max(len(story.items) - 1, 0),
            correspondent=correspondent_labels.get(slug)
            or (primary.source.name if primary else "Filed"),
            reading_time_minutes=None,
            difficulty=None,
        )
    return Card(
        post_id=post.id,
        kind=post.kind,
        destination=destination,
        title=post.title,
        summary=post.summary,
        # Denormalized at write time (pipeline.write_posts), so an article card
        # never loads its story's items to find a picture.
        banner=post.banner_url,
        at=post.generated_at,
        topics=list(post.topics or []),
        source_name=None,
        more_items=0,
        correspondent=None,
        reading_time_minutes=post.reading_time_minutes,
        difficulty=post.difficulty,
    )


def build_page(posts: list[Post], correspondent_labels: dict[str, str]) -> list[Card]:
    return [card for post in posts if (card := build(post, correspondent_labels)) is not None]


def as_json(card: Card) -> dict:
    data = asdict(card)
    data["at"] = card.at.isoformat() if card.at else None
    return data
