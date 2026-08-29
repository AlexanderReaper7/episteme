"""What filing refuses, and the two pure functions behind it.

`file_post` itself needs a database, which unit tests here do not have (see
conftest). What CAN be pinned without one is every guard that fires before the
first query, because those are the ones that decide whether a correspondent's
mistake becomes a broken post or an exception it can read. The database-side
behaviour - upsert by item key, one story per period, the body-iff-self-rendering
CHECK - is exercised against the real database inside a rolled-back transaction
instead, and what was watched is written in 0046.
"""

import math
from datetime import UTC, datetime

import pytest

from episteme.correspondents import FiledItem, FilingError, file_post
from episteme.correspondents.filing import _mean_unit

SOURCE = object()  # never reached: every test below raises before the first query


def _item(key="Matsedel/koppargrillen/2026w35", url="https://x.test/menu", **kw):
    return FiledItem(key=key, url=url, **kw)


def test_the_item_key_is_hashed_into_a_plain_digest():
    """`source_items.hash` keeps its shape (64 hex chars) and its uniqueness; only
    the choice of what goes into it moved to the producer (0046)."""
    digest = _item().hash
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    # The key itself is never stored, so two correspondents cannot collide by
    # accident and cannot read each other's keys back out either.
    assert _item(key="Matsedel/italia/2026w35").hash != digest


async def test_filing_nothing_is_refused():
    """A correspondent that read nothing is generating content, which is the
    writer's job. `item_count > 0` stays honest because of this line."""
    with pytest.raises(FilingError, match="at least one item"):
        await file_post(None, source=SOURCE, items=[], title="Lunch", href="/c/matsedel")


async def test_an_item_without_a_url_is_refused():
    """The URL is what makes the post accountable through /post/{id}/provenance."""
    with pytest.raises(FilingError, match="need a URL"):
        await file_post(
            None, source=SOURCE, items=[_item(url="")], title="Lunch", href="/c/matsedel"
        )


async def test_two_items_sharing_a_key_are_refused():
    """A key identifies an item. Two of them under one key would upsert onto each
    other, and the second would silently win."""
    with pytest.raises(FilingError, match="share one key"):
        await file_post(
            None,
            source=SOURCE,
            items=[_item(), _item(url="https://x.test/other")],
            title="Lunch",
            href="/c/matsedel",
        )


def test_the_centroid_is_a_unit_vector():
    """`pipeline.update_centroid` is the incremental form of this; a filed story is
    built in one go, so the batch form has to land in the same place."""
    centroid = _mean_unit([[1.0, 0.0], [0.0, 1.0]])
    assert math.isclose(sum(x * x for x in centroid) ** 0.5, 1.0)
    assert centroid[0] == pytest.approx(centroid[1])


def test_a_single_vector_survives_the_centroid_unchanged():
    assert _mean_unit([[0.6, 0.8]]) == pytest.approx([0.6, 0.8])


def test_a_filed_item_keeps_the_publish_date_it_was_given():
    at = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)
    assert _item(published_at=at).published_at == at
